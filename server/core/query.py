"""THE filter/sort/projection compiler. Pure: no database access.

One compiler, shared by list, count, saved-view execution, kanban grouping, the
association blocks, and (M6) the messaging command grammar. Two parsers would be
the most likely R1 violation in this milestone, and the second one would be the
one that forgets a check.

## The safety property

Only *values* are ever bound as parameters. Everything that becomes SQL text
comes from a closed source:

| Element          | Source                                    |
|------------------|-------------------------------------------|
| column name      | `FieldSpec.column` -- a Python literal     |
| custom key       | bound as `%s` into `core.jsonb_*(custom, %s)` |
| operator         | the `_OPS` template table                  |
| cast / expression| `sql_expression(kind)`                     |
| direction        | `{"asc": "ASC", "desc": "DESC"}`           |

The JSONB case is where naive implementations interpolate, and they never need
to: the key is an ordinary argument to the helper function, so it binds.

**A dependency worth stating:** psycopg2 binds parameters client-side, so
`core.jsonb_date(custom, %s)` reaches the server as
`core.jsonb_date(custom, 'renewal')` and still matches the promoted expression
index. Under a server-side-binding driver (psycopg3 in binary mode, asyncpg) it
would not, and every custom-field query would silently go sequential. There is
an EXPLAIN test pinning this.

## Unknown fields raise

An unrecognized filter key is a 400, never a dropped clause. A dropped filter
returns MORE rows than were asked for, which is a disclosure bug wearing a UX
bug's clothes -- the same reasoning as "gates raise, never no-op".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from server.core import dates, permissions, registry
from server.core.permissions import Principal
from server.core.registry import FieldSpec, UnknownField

# Bounds. Unbounded nesting or a 100k-element IN list is a cheap planner DoS.
MAX_DEPTH = 3
MAX_CLAUSES = 32
MAX_IN_VALUES = 500
MAX_LIMIT = 200
DEFAULT_LIMIT = 50

_DIRECTIONS = {"asc": "ASC", "desc": "DESC"}
_NULLS = {"asc": "NULLS LAST", "desc": "NULLS LAST"}

# Which cast helper serves each kind. Shared with the index promoter so the
# ORDER BY expression and the index expression are textually identical -- the
# only reason a promoted index is ever used (R1).
_CUSTOM_EXPR = {
    "number": "core.jsonb_num({alias}.custom, %s)",
    "currency": "core.jsonb_num({alias}.custom, %s)",
    "date": "core.jsonb_date({alias}.custom, %s)",
    "datetime": "core.jsonb_ts({alias}.custom, %s)",
    "boolean": "core.jsonb_bool({alias}.custom, %s)",
}
_CUSTOM_DEFAULT = "core.jsonb_text({alias}.custom, %s)"


def sql_expression(kind: str, alias: str = "t") -> str:
    """The SQL expression for a custom field of `kind`. One definition, used by
    both this compiler and server/core/custom_fields.py."""
    return _CUSTOM_EXPR.get(kind, _CUSTOM_DEFAULT).format(alias=alias)


# (kind, operator) -> (template, argument count). `*` matches any kind.
_OPS: dict[tuple[str, str], tuple[str, int]] = {}


def _register_ops() -> None:
    text_like = ("text", "url", "email", "phone")
    numeric = ("number", "currency")
    ordered = numeric + ("date", "datetime")

    for kind in text_like:
        _OPS[(kind, "eq")] = ("{lhs} = %s", 1)
        _OPS[(kind, "neq")] = ("{lhs} IS DISTINCT FROM %s", 1)
        # ESCAPE is explicit so a value containing % or _ is a literal, not a
        # wildcard. The value itself is escaped in _coerce().
        _OPS[(kind, "contains")] = ("{lhs} ILIKE %s ESCAPE '\\'", 1)
        _OPS[(kind, "starts_with")] = ("{lhs} ILIKE %s ESCAPE '\\'", 1)
        _OPS[(kind, "in")] = ("{lhs} = ANY(%s)", 1)

    for kind in ordered:
        _OPS[(kind, "eq")] = ("{lhs} = %s", 1)
        _OPS[(kind, "neq")] = ("{lhs} IS DISTINCT FROM %s", 1)
        _OPS[(kind, "gt")] = ("{lhs} > %s", 1)
        _OPS[(kind, "gte")] = ("{lhs} >= %s", 1)
        _OPS[(kind, "lt")] = ("{lhs} < %s", 1)
        _OPS[(kind, "lte")] = ("{lhs} <= %s", 1)
        _OPS[(kind, "between")] = ("{lhs} BETWEEN %s AND %s", 2)

    for kind in ("select", "uuid"):
        _OPS[(kind, "eq")] = ("{lhs} = %s", 1)
        _OPS[(kind, "neq")] = ("{lhs} IS DISTINCT FROM %s", 1)
        _OPS[(kind, "in")] = ("{lhs} = ANY(%s)", 1)

    _OPS[("boolean", "eq")] = ("{lhs} = %s", 1)
    _OPS[("multiselect", "contains")] = ("{lhs} @> %s::jsonb", 1)

    _OPS[("*", "is_null")] = ("{lhs} IS NULL", 0)
    _OPS[("*", "is_not_null")] = ("{lhs} IS NOT NULL", 0)


_register_ops()


def operators_for(kind: str) -> list[str]:
    return sorted(
        {op for (k, op) in _OPS if k == kind} | {op for (k, op) in _OPS if k == "*"}
    )


class FilterError(ValueError):
    """A malformed or disallowed filter. Always a 400, never a silent drop."""


@dataclass
class Compiled:
    sql: str
    params: list[Any]


# ── Left-hand side ───────────────────────────────────────────────────────────

def _lhs(spec: FieldSpec, alias: str) -> tuple[str, list[Any]]:
    """The SQL for a field reference, plus any bound parameters it carries.

    The only place a column name becomes SQL text. `spec.column` is a Python
    literal from the registry and has no path from user input; a custom key is
    bound, never interpolated.
    """
    if spec.column:
        return f"{alias}.{spec.column}", []
    return sql_expression(spec.kind, alias), [spec.custom_key]


def field_expression(
    entity: str, ref: str, *,
    principal: Principal, custom_fields: Iterable[dict[str, Any]] = (), alias: str = "t",
) -> Compiled:
    """The raw SQL expression for one field, with its own bound parameters --
    `compile_filter()`'s field-resolution step (spec lookup, filterable check,
    read-permission check, real-column-or-custom-jsonb `_lhs()`) factored out
    for a caller that wants the expression itself, not a comparison against
    it. `repository.aggregate()`'s GROUP BY/metric expressions are the first
    such caller, so a query-shaped need doesn't grow a second parser (R1).

    Filterable, not just readable, is still the right gate here: an
    unfilterable field is typically a `compute` field whose SQL expression
    doesn't actually exist (`registry.verify()` enforces this pairing), so
    grouping or aggregating by one would either error confusingly or (worse)
    silently read the wrong column, exactly the failure `compile_filter()`
    already guards against for a plain filter.
    """
    spec = registry.field_spec(entity, ref, custom_fields)
    if not spec.filterable:
        raise UnknownField(f"{ref} is not filterable")
    permissions.require_field_readable(principal, entity, ref)
    sql, params = _lhs(spec, alias)
    return Compiled(sql, params)


# ── Value coercion ───────────────────────────────────────────────────────────

def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _coerce_scalar(spec: FieldSpec, op: str, value: Any) -> Any:
    """Validate and convert one value. Raises FilterError on a type mismatch.

    Type validation happens here rather than being left to PostgreSQL, because
    the cast helpers deliberately return NULL for garbage -- so a bad filter
    value would silently match nothing instead of reporting a problem.
    """
    kind = spec.kind
    if value is None:
        return None

    if kind in ("number", "currency"):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise FilterError(f"{spec.name}: expected a number, got {type(value).__name__}")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise FilterError(f"{spec.name}: {value!r} is not a number") from exc

    if kind == "date":
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        try:
            # fromisoformat raises on "2026-13-45"; to_date would silently
            # return 2027-02-14 instead (safety rule 6).
            return date.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise FilterError(f"{spec.name}: {value!r} is not an ISO date") from exc

    if kind == "datetime":
        if isinstance(value, datetime):
            return value
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise FilterError(f"{spec.name}: {value!r} is not an ISO timestamp") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "1", "yes"):
            return True
        if str(value).lower() in ("false", "0", "no"):
            return False
        raise FilterError(f"{spec.name}: {value!r} is not a boolean")

    if kind == "select" and spec.options and str(value) not in spec.options:
        raise FilterError(
            f"{spec.name}: {value!r} is not one of {', '.join(spec.options)}"
        )

    if kind == "uuid":
        return str(value)

    text = str(value)
    if op == "contains":
        return f"%{_escape_like(text)}%"
    if op == "starts_with":
        return f"{_escape_like(text)}%"
    return text


def _coerce(spec: FieldSpec, op: str, value: Any, arity: int) -> list[Any]:
    if arity == 0:
        return []

    if op == "in":
        # A bare string here is the `attendees="alice@example.com"` bug: str is
        # iterable, so a permissive implementation explodes it into one clause
        # per character. Rejected explicitly rather than coerced.
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise FilterError(f"{spec.name}: 'in' needs a list of values")
        if not value:
            raise FilterError(f"{spec.name}: 'in' needs at least one value")
        if len(value) > MAX_IN_VALUES:
            raise FilterError(
                f"{spec.name}: 'in' accepts at most {MAX_IN_VALUES} values"
            )
        return [[_coerce_scalar(spec, "eq", v) for v in value]]

    if arity == 2:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise FilterError(f"{spec.name}: 'between' needs [low, high]")
        if len(value) != 2:
            raise FilterError(f"{spec.name}: 'between' needs exactly two values")
        return [_coerce_scalar(spec, op, v) for v in value]

    return [_coerce_scalar(spec, op, value)]


# ── Filters ──────────────────────────────────────────────────────────────────

def _compile_role_clause(
    entity: str, node: dict[str, Any], *, principal: Principal, alias: str,
) -> Compiled:
    """`{"role": "portfolio_of", "direction": "from"|"to", "as_of": "...",
    "include_history": false}` -- a clause with no `field`, compiled to an
    `EXISTS` against `core.associations` instead of a comparison on
    `entity`'s own row. This is what lets a saved view select "organizations
    that are portfolio companies" the same way it selects "organizations
    named Acme": generic across every registered role (R6 -- no per-vertical
    code here), the same reason `and`/`or`/`not` are branches in
    `compile_filter()` rather than a second parser.

    An association is readable only when BOTH endpoints are
    (`associations.py`'s module docstring), but this EXISTS never joins in
    the other side's row -- it only tests whether a matching edge exists, so
    it must check read access on the other side's entity TYPE itself, the
    same way an ordinary field clause calls `permissions.
    require_field_readable()` a few lines below in `compile_filter()`.
    Skipping this would make `{"role": "portfolio_of", "direction": "from"}`
    a disclosure oracle: a principal with no grant on `fund` at all could
    still learn which organizations have SOME fund relationship purely from
    which rows come back, via a `filters` value they can supply directly on
    any list/aggregate call or a saved view's stored `filters` (`core.
    saved_views.filters` is re-permission-checked on every execution, a
    promise this clause must keep).

    Defaults to "as of today" when `as_of` is omitted, and `include_history`
    must be set explicitly to see an ended association -- the same default
    `associations.edges_for()`/`roles_of()` already use. Getting this wrong
    silently would be a real bug for exactly the saved views this exists for:
    "Portfolio companies" divested five years ago must not still match.

    Queries the base table directly rather than `core.association_edges`:
    the caller already names which side `entity` sits on via `direction`, so
    there is no need for the view's self/other union.
    """
    role_name = node.get("role")
    direction = node.get("direction", "from")
    if not isinstance(role_name, str):
        raise FilterError("'role' filter needs a string role name")
    if direction not in ("from", "to"):
        raise FilterError(f"direction must be 'from' or 'to', got {direction!r}")

    role_spec = registry.role(role_name)  # raises UnknownField for a typo'd role
    if node.get("include_history"):
        when_sql, when_params = "TRUE", []
    else:
        try:
            as_of = dates.parse_date(node.get("as_of"), "as_of") or date.today()
        except dates.DateParseError as exc:
            raise FilterError(str(exc)) from exc
        when_sql, when_params = dates.overlap_clause(
            include_history=False, as_of=as_of, alias="a"
        )

    if role_spec.symmetric:
        # Canonicalized on write (endpoints sorted alphabetically), so
        # `entity`'s matching row can land on either side regardless of the
        # direction the caller asked for -- `direction` is meaningless for a
        # symmetric role, unlike an asymmetric one where it picks which
        # endpoint type to match. Every symmetric role connects same-typed
        # endpoints (from_types == to_types by construction), so `entity`
        # still names the type filter on both sides of the OR.
        if entity not in role_spec.from_types:
            raise FilterError(
                f"{role_name!r} does not connect to {entity!r} "
                f"(valid: {', '.join(role_spec.from_types)})"
            )
        for other in role_spec.from_types:
            principal.require("read", other)
        sql = (
            f"EXISTS (SELECT 1 FROM core.associations a "
            f"WHERE a.role = %s "
            f"AND ((a.from_type = %s AND a.from_id = {alias}.id) "
            f"OR (a.to_type = %s AND a.to_id = {alias}.id)) "
            f"AND ({when_sql}))"
        )
        return Compiled(sql, [role_name, entity, entity] + when_params)

    valid_types = role_spec.from_types if direction == "from" else role_spec.to_types
    if entity not in valid_types:
        raise FilterError(
            f"{role_name!r} does not connect as {direction!r} to {entity!r} "
            f"(valid: {', '.join(valid_types)})"
        )
    other_types = role_spec.to_types if direction == "from" else role_spec.from_types
    for other in other_types:
        principal.require("read", other)

    id_col, type_col = ("from_id", "from_type") if direction == "from" else ("to_id", "to_type")
    sql = (
        f"EXISTS (SELECT 1 FROM core.associations a "
        f"WHERE a.{type_col} = %s AND a.{id_col} = {alias}.id "
        f"AND a.role = %s AND ({when_sql}))"
    )
    return Compiled(sql, [entity, role_name] + when_params)


def compile_filter(
    entity: str,
    node: Optional[dict[str, Any]],
    *,
    principal: Principal,
    custom_fields: Iterable[dict[str, Any]] = (),
    alias: str = "t",
    _depth: int = 0,
    _budget: Optional[list[int]] = None,
) -> Compiled:
    """Compile a filter tree to `(sql, params)`.

        {"and": [{"field": "status", "op": "eq", "value": "open"},
                 {"field": "custom.renewal", "op": "gt", "value": "2026-01-01"}]}

    An empty or absent filter compiles to TRUE.
    """
    if node in (None, {}, []):
        return Compiled("TRUE", [])
    if _depth > MAX_DEPTH:
        raise FilterError(f"filters may nest at most {MAX_DEPTH} levels")
    budget = _budget if _budget is not None else [MAX_CLAUSES]

    for junction in ("and", "or"):
        if junction in node:
            children = node[junction]
            if not isinstance(children, (list, tuple)) or not children:
                raise FilterError(f"{junction!r} needs a non-empty list")
            parts, params = [], []
            for child in children:
                compiled = compile_filter(
                    entity, child, principal=principal, custom_fields=custom_fields,
                    alias=alias, _depth=_depth + 1, _budget=budget,
                )
                parts.append(compiled.sql)
                params.extend(compiled.params)
            return Compiled(f"({f' {junction.upper()} '.join(parts)})", params)

    if "not" in node:
        inner = compile_filter(
            entity, node["not"], principal=principal, custom_fields=custom_fields,
            alias=alias, _depth=_depth + 1, _budget=budget,
        )
        return Compiled(f"(NOT {inner.sql})", inner.params)

    if "role" in node:
        budget[0] -= 1
        if budget[0] < 0:
            raise FilterError(f"a filter may contain at most {MAX_CLAUSES} clauses")
        return _compile_role_clause(entity, node, principal=principal, alias=alias)

    budget[0] -= 1
    if budget[0] < 0:
        raise FilterError(f"a filter may contain at most {MAX_CLAUSES} clauses")

    ref, op = node.get("field"), node.get("op", "eq")
    if not isinstance(ref, str) or not isinstance(op, str):
        raise FilterError("each clause needs a string 'field' and 'op'")

    spec = registry.field_spec(entity, ref, custom_fields)
    if not spec.filterable:
        raise UnknownField(f"{ref} is not filterable")

    # Filtering on a masked field is a disclosure oracle: `salary > 200000`
    # returns the right rows even though salary never appears in the response,
    # so the value is readable by binary search. Checked here, at the same choke
    # point that resolves the field.
    permissions.require_field_readable(principal, entity, ref)

    template_arity = _OPS.get((spec.kind, op)) or _OPS.get(("*", op))
    if template_arity is None:
        raise FilterError(
            f"{op!r} is not valid on a {spec.kind} field "
            f"(valid: {', '.join(operators_for(spec.kind))})"
        )
    template, arity = template_arity

    lhs_sql, lhs_params = _lhs(spec, alias)
    values = _coerce(spec, op, node.get("value"), arity)
    return Compiled(template.format(lhs=lhs_sql), lhs_params + values)


# ── Sort ─────────────────────────────────────────────────────────────────────

def compile_sort(
    entity: str,
    sorts: Optional[Iterable[Any]],
    *,
    principal: Principal,
    custom_fields: Iterable[dict[str, Any]] = (),
    alias: str = "t",
) -> Compiled:
    """Compile a sort list to an ORDER BY clause.

    `t.id` is always appended so the order is total. Without it, two rows with
    equal sort keys can swap between pages, and a paginated list silently
    duplicates and skips rows -- which reads as a data bug, not a paging bug.
    """
    spec_entity = registry.entity(entity)
    requested = list(sorts) if sorts else [
        {"field": f, "direction": d} for f, d in spec_entity.default_sort
    ]

    parts, params = [], []
    for item in requested:
        if isinstance(item, str):
            item = {"field": item, "direction": "asc"}
        ref = item.get("field")
        direction = str(item.get("direction", "asc")).lower()
        if direction not in _DIRECTIONS:
            raise FilterError(f"sort direction must be asc or desc, got {direction!r}")

        spec = registry.field_spec(entity, ref, custom_fields)
        if not spec.sortable:
            raise UnknownField(f"{ref} is not sortable")
        permissions.require_field_readable(principal, entity, ref)

        lhs_sql, lhs_params = _lhs(spec, alias)
        parts.append(f"{lhs_sql} {_DIRECTIONS[direction]} {_NULLS[direction]}")
        params.extend(lhs_params)

    parts.append(f"{alias}.id DESC")
    return Compiled("ORDER BY " + ", ".join(parts), params)


# ── Projection ───────────────────────────────────────────────────────────────

def compile_projection(
    entity: str,
    select: Optional[Iterable[str]],
    *,
    principal: Principal,
    custom_fields: Iterable[dict[str, Any]] = (),
    alias: str = "t",
) -> list[str]:
    """Column list for a SELECT. Always includes the spine and `custom`; masked
    fields are excluded here as well as by mask_record(), so a masked value
    never leaves the database in the first place."""
    spec_entity = registry.entity(entity)
    perms = principal.for_object(entity)

    if select:
        names = list(select)
        for ref in names:
            registry.field_spec(entity, ref, custom_fields)
            permissions.require_field_readable(principal, entity, ref)
    else:
        names = [
            name for name, fspec in spec_entity.fields.items()
            if fspec.column and perms.readable_field(name)
        ]

    columns = {f"{alias}.{spec_entity.fields[n].column}" for n in names
               if n in spec_entity.fields and spec_entity.fields[n].column}
    for required in ("id", "org_id", "owner_id", "created_at", "updated_at"):
        if required in spec_entity.fields or required in ("org_id",):
            columns.add(f"{alias}.{required}")
    columns.add(f"{alias}.custom")
    return sorted(columns)


def clamp_limit(limit: Optional[int]) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise FilterError(f"limit must be an integer, got {limit!r}") from exc
    if value < 1:
        raise FilterError("limit must be at least 1")
    return min(value, MAX_LIMIT)

"""THE generic CRUD path (R4).

Every read and write of every registered entity goes through here. This is the
one place permission checks, visibility predicates, field masking, type
validation and event emission are applied — so each rule exists once and is
provably applied everywhere.

Nothing else in the codebase opens a cursor against a record table. A module
that wants its own behavior subscribes to an event or declares a registry hook;
it does not write SQL.

## The choke points

| Operation | Applied |
|---|---|
| `list` / `get` / `count` | `require('read')` → read predicate in WHERE → `_finalize_read()` |
| `create` | `require('create')` → `reject_masked_writes()` → owner forced → `_finalize_read()` |
| `update` | `require('edit')` → `reject_masked_writes()` → **edit** predicate in the UPDATE → `_finalize_read()` |
| `delete` | `require('delete')` → **delete** predicate in the DELETE |

`_finalize_read()` is `mask_record()` (role-level field masks, uniform per
entity) plus a second pass for `FieldSpec.read_level` (row-owner field
scoping, e.g. `interaction.body` under safety rule 10) -- orthogonal checks,
both applied every time a row leaves this module.

The subtlety on update and delete: a user may be able to *read* a row and not
edit it, so re-checking the read predicate would wrongly allow the write. The
edit predicate goes into the statement itself and the row count is asserted —
which also avoids the read-then-check-then-write race entirely.

## 404 versus 403

A row the principal cannot see is `NotFound`, never `PermissionDenied`. A 403
on an invisible row confirms it exists, which is exactly what the visibility
level was meant to hide. A row they *can* see but not edit is `PermissionDenied`,
because that is not a secret.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from psycopg2.extras import Json

from server.core import events, permissions, query, registry
from server.core.permissions import Principal
from server.core.registry import UnknownField
from server.db import pool


class NotFound(LookupError):
    """No such row, or not visible to this principal. The two are deliberately
    indistinguishable from outside."""


class Conflict(RuntimeError):
    """Optimistic concurrency failure — the row changed since it was read."""


class ValidationError(ValueError):
    """A field value that cannot be stored. A ValueError so it triggers the same
    repair-retry path as extraction failures (safety rule 6)."""


def _finalize_read(principal: Principal, entity: str, record: dict[str, Any]) -> dict[str, Any]:
    """The one place a row is prepared for return to a caller -- role-level
    field masking, then row-owner field scoping (safety rule 10).

    `mask_record()` handles role-level masks, uniform across every row of an
    entity. `FieldSpec.read_level` is a second, orthogonal check on top of
    that: a field like `interaction.body` is visible only to the row's own
    owner (or an admin), regardless of the principal's overall read_level.
    Applied at every point a row leaves the repository -- list/get/fetch AND
    the row create()/update() hand back -- so an editor without ownership
    cannot see a body-like field in a write response either.
    """
    record = permissions.mask_record(principal, entity, record)
    owner_id = record.get("owner_id")
    for name, fspec in registry.entity(entity).fields.items():
        if fspec.read_level and name in record:
            if not permissions.satisfies_level(principal, fspec.read_level, owner_id):
                record[name] = None
    return record


# ── Custom-field context ─────────────────────────────────────────────────────

def _custom_fields(cur, entity: str) -> list[dict[str, Any]]:
    """This org's custom fields for one entity. Read inside the caller's
    transaction so it is tenant-scoped by RLS like everything else.

    Selects the union of what every caller needs: the write/query path
    (`entity`, `key`, `kind`, `options`, `archived_at`) and the read-only
    schema-introspection path (`label`, `indexed`) that `custom_field_defs`
    exposes over the API — one query, not two near-duplicates.
    """
    cur.execute(
        "SELECT entity, key, kind, label, options, indexed, archived_at "
        "FROM core.custom_fields WHERE entity = %s AND archived_at IS NULL "
        "ORDER BY created_at",
        (entity,),
    )
    return [dict(r) for r in cur.fetchall()]


def custom_field_defs(principal: Principal, entity: str) -> list[dict[str, Any]]:
    """Custom field DEFINITIONS for `entity` — key/kind/label/options/indexed —
    readable by anyone who can read `entity` itself.

    Deliberately NOT routed through `list_records(principal, "custom_field",
    ...)`: `custom_field` is `admin_only`, correctly, because *creating or
    editing* a definition is a schema change. But a member building a table or
    a create form for `person` needs to know what custom fields exist on
    `person` regardless of whether they personally administer the schema —
    gating that behind admin would mean a non-admin's table silently never
    shows a custom column anyone else can see the data in.
    """
    registry.entity(entity)  # raises UnknownEntity for a bad name
    principal.require("read", entity)
    with pool.transaction(
        org_id=principal.org_id, user_id=principal.user_id, readonly=True
    ) as cur:
        return _custom_fields(cur, entity)


# ── Value validation ─────────────────────────────────────────────────────────

def _validate(spec: registry.FieldSpec, value: Any) -> Any:
    """Coerce and validate one value for storage.

    Every field is validated, including types — not just the obviously risky
    ones. A sibling project validated only date and time, and shipped
    `duration_minutes="90"` raising deep inside timedelta, `"2026-13-45"`
    passing a format regex, and a bare string where a list was expected being
    exploded by `list()` into one calendar attendee per character, each sent as
    a real meeting invite.
    """
    if value is None:
        return None
    kind = spec.kind

    if kind in ("number", "currency"):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValidationError(f"{spec.name}: expected a number")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{spec.name}: {value!r} is not a number") from exc

    if kind == "date":
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        try:
            # fromisoformat rejects "2026-13-45"; to_date would silently return
            # 2027-02-14 instead.
            return date.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{spec.name}: {value!r} is not an ISO date") from exc

    if kind == "datetime":
        if isinstance(value, datetime):
            return value
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{spec.name}: {value!r} is not a timestamp") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    if kind == "boolean":
        if isinstance(value, bool):
            return value
        raise ValidationError(f"{spec.name}: expected a boolean")

    if kind == "select":
        text = str(value)
        if spec.options and text not in spec.options:
            raise ValidationError(
                f"{spec.name}: {text!r} is not one of {', '.join(spec.options)}"
            )
        return text

    if kind == "multiselect":
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValidationError(f"{spec.name}: expected a list")
        items = list(value)
        if spec.options:
            bad = [v for v in items if v not in spec.options]
            if bad:
                raise ValidationError(
                    f"{spec.name}: {bad!r} not in {', '.join(spec.options)}"
                )
        # Returned as a plain list, NOT Json()-wrapped: this same _validate()
        # also runs for custom-field values, which get merged into `custom`
        # and serialized once via json.dumps() -- wrapping here corrupted
        # them (Json.__str__ produces a quoted SQL literal, not JSON, so
        # json.dumps(..., default=str) silently stored the wrong string).
        # Real jsonb-column writes are wrapped at the one place that binds a
        # column value as a psycopg2 param -- see create()/update().
        return items

    if kind == "uuid":
        try:
            return str(uuid.UUID(str(value)))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{spec.name}: {value!r} is not a uuid") from exc

    if kind == "jsonb":
        return value

    if isinstance(value, (list, dict)):
        raise ValidationError(f"{spec.name}: expected a scalar, got {type(value).__name__}")
    return str(value)


def _column_params(spec: registry.EntitySpec, columns: dict[str, Any]) -> list[Any]:
    """Build the SQL parameter list for `columns`, in order.

    The one place a jsonb/multiselect-kind column value is wrapped in
    `Json()` -- `_validate()` deliberately returns a plain Python list/dict
    for those kinds, because it is shared with custom-field validation,
    whose values are merged into `custom` and serialized once via
    `json.dumps()` rather than bound as individual psycopg2 params. Wrapping
    inside `_validate()` corrupted custom fields of these kinds (`Json`'s
    `__str__` is a quoted SQL literal, not JSON, so `json.dumps(...,
    default=str)` silently stored the wrong string). Real columns need the
    wrapper; `custom` doesn't -- this is the one place both callers'
    payloads finally diverge for real.
    """
    params = []
    for name in columns:
        value = columns[name]
        fspec = spec.fields.get(name)
        if fspec is not None and fspec.kind in ("jsonb", "multiselect") and value is not None:
            value = Json(value)
        params.append(value)
    return params


def _split_changes(
    entity: str,
    changes: dict[str, Any],
    custom_fields: Iterable[dict[str, Any]],
    principal: Principal,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split user-supplied changes into (column values, custom values),
    validating each and refusing anything not writable."""
    spec = registry.entity(entity)
    columns: dict[str, Any] = {}
    custom: dict[str, Any] = {}

    for name, value in changes.items():
        if name == "custom":
            if not isinstance(value, dict):
                raise ValidationError("custom must be an object")
            for key, sub in value.items():
                fspec = registry.field_spec(entity, f"custom.{key}", custom_fields)
                custom[key] = _validate(fspec, sub)
            continue

        if name in registry.SYSTEM_FIELDS:
            raise UnknownField(f"{name} is not writable")
        fspec = spec.field(name)
        # writable=False means "not writable by a human or API caller" --
        # not "the platform itself can never set it." A system principal
        # (permissions.system_principal(), always audited via actor_kind in
        # the event log) is how derived data actually gets written: e.g.
        # server/core/derivation.py setting `person.source='derived'` on a
        # channel-materialized contact. Without this, the only way to write
        # a writable=False field at all would be a second, hand-rolled SQL
        # path outside the repository -- exactly what R1 exists to prevent.
        if not fspec.writable and not principal.is_system:
            raise UnknownField(f"{name} is not writable")
        # owner_id declares write_level='team': without this a user at 'own'
        # level can reassign a record away from themselves (losing it) or to
        # themselves (stealing it).
        if fspec.write_level != "own":
            level = principal.for_object(entity).level_for("edit")
            if permissions.widest(level, fspec.write_level) != level:
                raise permissions.PermissionDenied(
                    "edit", entity, f"field {name!r} requires {fspec.write_level} access"
                )
        columns[name] = _validate(fspec, value)

    return columns, custom


# ── Read ─────────────────────────────────────────────────────────────────────

def list_records(
    principal: Principal,
    entity: str,
    *,
    filters: Optional[dict[str, Any]] = None,
    sort: Optional[list[Any]] = None,
    select: Optional[list[str]] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> dict[str, Any]:
    principal.require("read", entity)
    spec = registry.entity(entity)
    if spec.admin_only:
        principal.require_admin(f"{entity} is administrative")
    limit = query.clamp_limit(limit)

    with pool.transaction(
        org_id=principal.org_id, user_id=principal.user_id, readonly=True
    ) as cur:
        custom = _custom_fields(cur, entity)
        where, params = _read_scope(principal, entity, filters, custom)
        order = query.compile_sort(
            entity, sort, principal=principal, custom_fields=custom
        )
        columns = query.compile_projection(
            entity, select, principal=principal, custom_fields=custom
        )

        cur.execute(
            f"SELECT {', '.join(columns)} FROM {spec.table} t "
            f"WHERE {where} {order.sql} LIMIT %s OFFSET %s",
            params + order.params + [limit, max(0, int(offset))],
        )
        rows = [_finalize_read(principal, entity, dict(r)) for r in cur.fetchall()]

        # Counted under the same predicate. A count that ignores visibility
        # leaks how many rows the principal cannot see.
        cur.execute(f"SELECT count(*) AS n FROM {spec.table} t WHERE {where}", params)
        total = int((cur.fetchone() or {}).get("n") or 0)

    return {"records": rows, "total": total, "limit": limit, "offset": offset}


def _read_scope(
    principal: Principal,
    entity: str,
    filters: Optional[dict[str, Any]],
    custom: Iterable[dict[str, Any]],
) -> tuple[str, list[Any]]:
    visible_sql, visible_params = permissions.visibility_predicate(
        principal, entity, "read"
    )
    compiled = query.compile_filter(
        entity, filters, principal=principal, custom_fields=custom
    )
    return f"({visible_sql}) AND ({compiled.sql})", list(visible_params) + compiled.params


def get_record(principal: Principal, entity: str, record_id: str) -> dict[str, Any]:
    principal.require("read", entity)
    spec = registry.entity(entity)
    visible_sql, visible_params = permissions.visibility_predicate(
        principal, entity, "read"
    )
    with pool.transaction(
        org_id=principal.org_id, user_id=principal.user_id, readonly=True
    ) as cur:
        cur.execute(
            f"SELECT t.* FROM {spec.table} t WHERE t.id = %s AND ({visible_sql})",
            [record_id] + list(visible_params),
        )
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"no {entity} {record_id}")
    return _finalize_read(principal, entity, dict(row))


def fetch_many(
    principal: Principal, entity: str, ids: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Batch fetch by id, respecting visibility. Used by the association
    hydrator so a record page is one query per entity type rather than one per
    related row — and so related records the principal cannot read are simply
    absent, rather than joined in behind the repository's back."""
    ids = [str(i) for i in ids if i]
    if not ids:
        return {}
    principal.require("read", entity)
    spec = registry.entity(entity)
    visible_sql, visible_params = permissions.visibility_predicate(
        principal, entity, "read"
    )
    with pool.transaction(
        org_id=principal.org_id, user_id=principal.user_id, readonly=True
    ) as cur:
        # ::uuid[] is required: psycopg2 sends a Python list of str as text[],
        # and `uuid = text` has no operator.
        cur.execute(
            f"SELECT t.* FROM {spec.table} t "
            f"WHERE t.id = ANY(%s::uuid[]) AND ({visible_sql})",
            [ids] + list(visible_params),
        )
        rows = [_finalize_read(principal, entity, dict(r)) for r in cur.fetchall()]
    return {str(r["id"]): r for r in rows}


# ── Write ────────────────────────────────────────────────────────────────────

def create(
    principal: Principal, entity: str, values: dict[str, Any]
) -> dict[str, Any]:
    perms = principal.require("create", entity)
    spec = registry.entity(entity)
    if spec.admin_only:
        principal.require_admin(f"{entity} is administrative")

    # Only USER-supplied changes are mask-checked. Including system columns
    # would make creation impossible whenever a defaulted field is masked.
    permissions.reject_masked_writes(principal, entity, values)

    record_id = str(uuid.uuid4())
    with pool.transaction(org_id=principal.org_id, user_id=principal.user_id) as cur:
        custom_defs = _custom_fields(cur, entity)
        columns, custom = _split_changes(entity, values, custom_defs, principal)

        for name, fspec in spec.fields.items():
            if fspec.required and not columns.get(name):
                raise ValidationError(f"{name} is required")

        # A platform-originated record has no human owner until someone claims
        # it -- and SYSTEM_USER_ID is not a real users row, so defaulting to it
        # would violate the foreign key. Unowned means own-level users cannot
        # see it, which is the right default for a record nobody has adopted.
        columns.setdefault(
            "owner_id", None if principal.is_system else principal.user_id
        )
        _derive_normalized(entity, columns)

        names = ["id", "org_id", "custom"] + list(columns)
        placeholders = ", ".join(["%s"] * len(names))
        params = [record_id, principal.org_id, json.dumps(custom, default=str)] + \
            _column_params(spec, columns)
        cur.execute(
            f"INSERT INTO {spec.table} ({', '.join(names)}) VALUES ({placeholders}) "
            f"RETURNING *",
            params,
        )
        row = dict(cur.fetchone())

        _run_validators(principal, entity, "create", record_id, before=None, after=row)

        event = _event(principal, entity, record_id, events.CREATED, after=row)
        event_id = events.record_event(cur, event)

    _publish(event, event_id)
    return _finalize_read(principal, entity, row)


def update(
    principal: Principal,
    entity: str,
    record_id: str,
    changes: dict[str, Any],
    *,
    clear: Iterable[str] = (),
    if_unmodified_since: Optional[Any] = None,
) -> dict[str, Any]:
    """Update a row.

    `clear` names custom keys to remove. It exists because `custom || %s` is a
    shallow merge, so without an explicit clear list a custom field could be set
    but never blanked — safety rule 2 (read-merge-write needs an explicit
    clearable-key set) reappearing where nobody expects it.

    `if_unmodified_since` is the row's `updated_at`. Inline in-cell editing
    makes concurrent edits routine, so optimistic concurrency ships now rather
    than being retrofitted through every call site later.
    """
    principal.require("edit", entity)
    spec = registry.entity(entity)
    if spec.admin_only:
        principal.require_admin(f"{entity} is administrative")

    clear = [c.partition(".")[2] if c.startswith("custom.") else c for c in clear]
    permissions.reject_masked_writes(
        principal, entity, changes, clear=[f"custom.{c}" for c in clear]
    )

    edit_sql, edit_params = permissions.visibility_predicate(principal, entity, "edit")

    with pool.transaction(org_id=principal.org_id, user_id=principal.user_id) as cur:
        custom_defs = _custom_fields(cur, entity)
        columns, custom = _split_changes(entity, changes, custom_defs, principal)
        _derive_normalized(entity, columns)

        cur.execute(
            f"SELECT t.* FROM {spec.table} t WHERE t.id = %s FOR UPDATE", (record_id,)
        )
        before = cur.fetchone()
        if before is None:
            raise NotFound(f"no {entity} {record_id}")
        before = dict(before)

        if if_unmodified_since is not None and str(before["updated_at"]) != str(
            if_unmodified_since
        ):
            raise Conflict(f"{entity} {record_id} changed since it was read")

        assignments = [f"{name} = %s" for name in columns]
        params: list[Any] = _column_params(spec, columns)
        if custom or clear:
            assignments.append("custom = (t.custom - %s::text[]) || %s::jsonb")
            params.extend([list(clear), json.dumps(custom, default=str)])
        assignments.append("updated_at = now()")

        cur.execute(
            f"UPDATE {spec.table} t SET {', '.join(assignments)} "
            f"WHERE t.id = %s AND ({edit_sql}) RETURNING *",
            params + [record_id] + list(edit_params),
        )
        row = cur.fetchone()
        if row is None:
            # The row exists (we locked it) but is outside the edit scope. It
            # was readable enough to reach here, so saying so is not a leak.
            raise permissions.PermissionDenied("edit", entity, f"record {record_id}")
        after = dict(row)

        _run_validators(principal, entity, "update", record_id, before=before, after=after)

        event = _event(
            principal, entity, record_id, events.UPDATED,
            before=before, after=after, diff=events.diff_of(before, after),
        )
        event_id = events.record_event(cur, event)

    _publish(event, event_id)
    return _finalize_read(principal, entity, after)


def delete(principal: Principal, entity: str, record_id: str) -> bool:
    principal.require("delete", entity)
    spec = registry.entity(entity)
    if spec.admin_only:
        principal.require_admin(f"{entity} is administrative")

    delete_sql, delete_params = permissions.visibility_predicate(
        principal, entity, "delete"
    )
    read_sql, read_params = permissions.visibility_predicate(principal, entity, "read")

    with pool.transaction(org_id=principal.org_id, user_id=principal.user_id) as cur:
        cur.execute(
            f"DELETE FROM {spec.table} t WHERE t.id = %s AND ({delete_sql}) RETURNING *",
            [record_id] + list(delete_params),
        )
        row = cur.fetchone()
        if row is None:
            # Disambiguate, because 403 on an invisible row confirms it exists.
            cur.execute(
                f"SELECT 1 FROM {spec.table} t WHERE t.id = %s AND ({read_sql})",
                [record_id] + list(read_params),
            )
            if cur.fetchone() is None:
                raise NotFound(f"no {entity} {record_id}")
            raise permissions.PermissionDenied("delete", entity, f"record {record_id}")

        before = dict(row)

        _run_validators(principal, entity, "delete", record_id, before=before, after=None)

        event = _event(principal, entity, record_id, events.DELETED, before=before)
        event_id = events.record_event(cur, event)

    _publish(event, event_id)
    return True


# ── Write-time validators ────────────────────────────────────────────────────

def _run_validators(
    principal: Principal,
    entity: str,
    action: str,
    record_id: Optional[str],
    *,
    before: Optional[dict[str, Any]],
    after: Optional[dict[str, Any]],
) -> None:
    """Run every registered validator for `entity`/`action`, inside the
    caller's transaction, after the row has been written but before that
    transaction commits.

    A validator raises to abort. The exception propagates straight out of this
    function and out of the enclosing `with pool.transaction(...)` block in
    create/update/delete, which rolls back and re-raises -- there is no
    try/except here to catch it, deliberately. `registry.py` owns the
    mechanism (registration, ordering, the `ValidationContext` shape);
    `repository.py` only calls it at the three points where a row's final
    state is known.

    A validator that itself calls back into `repository.*` (as
    `modules/investor_portal`'s commitment gate does, reading `document` rows)
    borrows a SECOND connection from the pool while the outer write still
    holds its own -- both are ordinary reads and never touch the same row, so
    there is no deadlock on locks, only on pool exhaustion if nested calls run
    deeper than `CRM_DB_POOL_MAX` (default 10) allows concurrently. Fine at
    today's scale; a validator that fans out to many nested repository calls
    should be treated as a smell, not a pattern to repeat.
    """
    validators = registry.validators_for(entity, action)
    if not validators:
        return
    context = registry.ValidationContext(
        principal=principal, entity=entity, action=action,
        record_id=record_id, before=before, after=after,
    )
    for validator in validators:
        validator(context)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _derive_normalized(entity: str, columns: dict[str, Any]) -> None:
    """Maintain the normalized shadow columns used for matching. Uses
    core/identity.py rather than inlining a .lower().strip() (R5)."""
    from server.core import identity

    if entity == "organization" and "name" in columns:
        columns["name_normalized"] = identity.normalize_name(columns["name"]) or ""
    if entity == "person":
        if "full_name" in columns:
            columns["name_normalized"] = identity.normalize_name(columns["full_name"]) or ""
        if "primary_email" in columns and columns["primary_email"]:
            normalized = identity.normalize_email(columns["primary_email"])
            if normalized is None:
                raise ValidationError(
                    f"{columns['primary_email']!r} is not a valid email address"
                )
            columns["primary_email"] = normalized


def _event(
    principal: Principal,
    entity: str,
    record_id: str,
    action: str,
    *,
    before: Optional[dict[str, Any]] = None,
    after: Optional[dict[str, Any]] = None,
    diff: Optional[dict[str, Any]] = None,
) -> events.RecordEvent:
    return events.RecordEvent(
        org_id=principal.org_id,
        entity=entity,
        record_id=record_id,
        action=action,
        actor_user_id=principal.user_id,
        actor_kind=f"system:{principal.system_reason}" if principal.is_system else "user",
        diff=diff or {},
        before=before or {},
        after=after or {},
        principal=principal,
    )


def _publish(event: events.RecordEvent, event_id: Optional[str]) -> None:
    """Dispatch after the transaction has committed. `record_event` returns None
    for a no-op update, and there is then nothing to publish."""
    if event_id is None:
        return
    events.publish_committed(
        events.RecordEvent(
            org_id=event.org_id, entity=event.entity, record_id=event.record_id,
            action=event.action, actor_user_id=event.actor_user_id,
            actor_kind=event.actor_kind, diff=event.diff, before=event.before,
            after=event.after, event_id=event_id, principal=event.principal,
        )
    )

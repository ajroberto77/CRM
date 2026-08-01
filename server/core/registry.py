"""The entity registry (R4). What an entity *is*, declared once.

The repository, the REST router and the UI are all driven by this. A new entity
is an entry here plus a table in `db/schema.py` -- never a new module, router
and component set. Hand-written CRUD for a registered entity is a bug.

Modules register their own entities and association roles through the same
functions core uses, which is what lets `modules/funds` add commitments without
editing anything under `server/core/` (R6).

## Why `verify()` exists

`permissions.visibility_predicate()` hardcodes `owner_id`, so a registered
entity whose table lacks that column produces invalid SQL -- but only when a
*non-admin* queries it, because admins short-circuit to `all`. Every test
written with the `admin` fixture passes. So the invariant is asserted at
startup, next to `schema.verify_rls()`, rather than discovered in production.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# Field kinds. Closed vocabulary: the filter compiler indexes its operator table
# on (kind, operator), and the index promoter picks a cast helper from it.
KINDS = (
    "text", "number", "date", "datetime", "boolean",
    "select", "multiselect", "currency", "url", "email", "phone", "uuid", "jsonb",
)

# Custom-field keys reach SQL as bound JSONB paths and as index name components.
# Constraining them at creation is what makes both structurally safe -- a key
# like `x' OR '1'='1` would be a stored injection that never looks like a
# request parameter.
CUSTOM_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,48}$")

# Every record table carries these. Enforced by verify().
REQUIRED_COLUMNS = ("id", "org_id", "owner_id", "custom", "created_at", "updated_at")

# The spine is never user-writable; the repository sets it.
SYSTEM_FIELDS = frozenset({"id", "org_id", "created_at", "updated_at"})


class RegistryError(RuntimeError):
    pass


class UnknownEntity(KeyError):
    pass


class UnknownField(ValueError):
    """Raised for a field that is not registered or not usable this way.

    A ValueError subclass on purpose: an unknown filter key is a 400, and it
    must never be silently dropped. A dropped filter returns MORE rows than
    were asked for -- a disclosure bug wearing a UX bug's clothes, and the same
    "raise, never no-op" rule the gates follow.
    """


@dataclass(frozen=True)
class FieldSpec:
    """One field on one entity. `column` is a real column; `custom_key` is a
    JSONB key. Exactly one is set."""
    name: str
    kind: str
    column: Optional[str] = None
    custom_key: Optional[str] = None
    filterable: bool = True
    sortable: bool = True
    writable: bool = True
    required: bool = False
    options: tuple[str, ...] = ()
    # Visibility level a principal needs to write this field. `owner_id`
    # declares "team": otherwise a user at `own` level can reassign a record
    # away from themselves (losing it) or to themselves (stealing it).
    write_level: str = "own"

    @property
    def is_custom(self) -> bool:
        return self.custom_key is not None


@dataclass(frozen=True)
class EntitySpec:
    name: str
    table: str
    fields: dict[str, FieldSpec]
    label: str = ""
    label_field: str = "name"
    default_sort: tuple[tuple[str, str], ...] = (("updated_at", "desc"),)
    searchable: tuple[str, ...] = ()
    supports_custom_fields: bool = True
    admin_only: bool = False
    module: str = "core"

    def field(self, name: str) -> FieldSpec:
        spec = self.fields.get(name)
        if spec is None:
            raise UnknownField(f"{self.name} has no field {name!r}")
        return spec


@dataclass(frozen=True)
class AssociationRole:
    """A relationship role. Declared per module so `modules/funds` can add
    `lp_in` without core knowing what a fund is (R6).

    `symmetric` roles are canonicalized on write (endpoints sorted) so `A co_
    investor_in B` and `B co_investor_in A` cannot both exist -- otherwise the
    uniqueness index never fires and the relationship is double-counted.
    """
    name: str
    from_types: tuple[str, ...]
    to_types: tuple[str, ...]
    inverse_label: str = ""
    symmetric: bool = False
    module: str = "core"


# ── The singleton ────────────────────────────────────────────────────────────
# Module-level, with an explicit reset() for tests -- mirroring
# config.reload_config(). Not rebuilt per request, and not a second cache (R5).

_ENTITIES: dict[str, EntitySpec] = {}
_ROLES: dict[str, AssociationRole] = {}


def register(spec: EntitySpec) -> EntitySpec:
    existing = _ENTITIES.get(spec.name)
    if existing is not None and existing.module != spec.module:
        raise RegistryError(
            f"entity {spec.name!r} already registered by module {existing.module!r}"
        )
    _ENTITIES[spec.name] = spec
    return spec


def register_role(role: AssociationRole) -> AssociationRole:
    existing = _ROLES.get(role.name)
    if existing is not None and existing.module != role.module:
        raise RegistryError(
            f"association role {role.name!r} already registered by module "
            f"{existing.module!r}"
        )
    _ROLES[role.name] = role
    return role


def entity(name: str) -> EntitySpec:
    spec = _ENTITIES.get(name)
    if spec is None:
        raise UnknownEntity(f"unknown entity {name!r} (known: {', '.join(entities())})")
    return spec


def entities() -> list[str]:
    return sorted(_ENTITIES)


def all_entities() -> list[EntitySpec]:
    return [_ENTITIES[name] for name in entities()]


def role(name: str) -> AssociationRole:
    spec = _ROLES.get(name)
    if spec is None:
        raise UnknownField(
            f"unknown association role {name!r} (known: {', '.join(sorted(_ROLES))})"
        )
    return spec


def roles() -> list[AssociationRole]:
    return [_ROLES[name] for name in sorted(_ROLES)]


def reset() -> None:
    """Drop every registration. Tests only."""
    _ENTITIES.clear()
    _ROLES.clear()


# ── Field resolution ─────────────────────────────────────────────────────────

def field_spec(entity_name: str, ref: str, custom: Iterable[dict[str, Any]] = ()) -> FieldSpec:
    """Resolve a field reference to a spec.

    `ref` is either a declared field name or `custom.<key>`. `custom` is the
    org's `core.custom_fields` rows, passed in rather than queried here so this
    module stays free of database access and the compiler can be tested without
    PostgreSQL.

    Raises UnknownField for anything unrecognized -- never returns None, and
    never falls back to treating the reference as a literal column name.
    """
    spec = entity(entity_name)
    if not ref.startswith("custom."):
        return spec.field(ref)

    if not spec.supports_custom_fields:
        raise UnknownField(f"{entity_name} does not support custom fields")
    key = ref.partition(".")[2]
    if not CUSTOM_KEY_RE.match(key):
        raise UnknownField(f"{key!r} is not a valid custom field key")
    for row in custom:
        if row["entity"] == entity_name and row["key"] == key and not row.get("archived_at"):
            return FieldSpec(
                name=ref,
                kind=row["kind"],
                custom_key=key,
                options=tuple(row.get("options") or ()),
            )
    raise UnknownField(f"{entity_name} has no custom field {key!r}")


# ── Startup verification ─────────────────────────────────────────────────────

def verify(tables: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Check every registration against the real schema. Empty list means sound.

    Called from the app lifespan beside schema.verify_rls(). Catches the
    owner_id case described in the module docstring, plus a field declared over
    a column that does not exist -- which would otherwise surface as a SQL error
    on whichever request first selected it.
    """
    problems: list[dict[str, Any]] = []
    for spec in all_entities():
        columns = tables.get(spec.table)
        if columns is None:
            problems.append({"entity": spec.name, "problem": "table_missing",
                             "table": spec.table})
            continue
        for required in REQUIRED_COLUMNS:
            if required not in columns:
                problems.append({"entity": spec.name, "problem": "missing_column",
                                 "column": required})
        for name, fspec in spec.fields.items():
            if fspec.kind not in KINDS:
                problems.append({"entity": spec.name, "problem": "unknown_kind",
                                 "field": name, "kind": fspec.kind})
            if fspec.column and fspec.column not in columns:
                problems.append({"entity": spec.name, "problem": "column_missing",
                                 "field": name, "column": fspec.column})
        for field_name, direction in spec.default_sort:
            if field_name not in spec.fields:
                problems.append({"entity": spec.name, "problem": "sort_field_missing",
                                 "field": field_name})
            if direction not in ("asc", "desc"):
                problems.append({"entity": spec.name, "problem": "bad_sort_direction",
                                 "direction": direction})

    known = set(entities())
    for role_spec in roles():
        for side in role_spec.from_types + role_spec.to_types:
            if side not in known:
                problems.append({"role": role_spec.name, "problem": "unknown_entity",
                                 "entity": side})
    return problems


# ── Core entities ────────────────────────────────────────────────────────────

def _spine(extra: dict[str, FieldSpec]) -> dict[str, FieldSpec]:
    """The columns every record entity carries. Declared once here rather than
    repeated per entity (R1)."""
    base = {
        "id": FieldSpec("id", "uuid", column="id", writable=False),
        "owner_id": FieldSpec("owner_id", "uuid", column="owner_id", write_level="team"),
        "created_at": FieldSpec("created_at", "datetime", column="created_at", writable=False),
        "updated_at": FieldSpec("updated_at", "datetime", column="updated_at", writable=False),
    }
    base.update(extra)
    return base


def register_core_entities() -> None:
    """Declare the domain-neutral core. Idempotent."""
    register(EntitySpec(
        name="organization", table="core.organizations", label="Companies",
        label_field="name", searchable=("name", "domain"),
        fields=_spine({
            "name": FieldSpec("name", "text", column="name", required=True),
            "domain": FieldSpec("domain", "text", column="domain"),
            "description": FieldSpec("description", "text", column="description"),
            "is_internal": FieldSpec("is_internal", "boolean", column="is_internal"),
            "source": FieldSpec("source", "select", column="source", writable=False,
                                options=("human", "derived", "import", "sync", "ai")),
            "is_derived": FieldSpec("is_derived", "boolean", column="is_derived",
                                    writable=False),
        }),
    ))

    register(EntitySpec(
        name="person", table="core.persons", label="People",
        label_field="full_name", searchable=("full_name", "primary_email"),
        fields=_spine({
            "full_name": FieldSpec("full_name", "text", column="full_name", required=True),
            "title": FieldSpec("title", "text", column="title"),
            "primary_email": FieldSpec("primary_email", "email", column="primary_email"),
            "description": FieldSpec("description", "text", column="description"),
            "source": FieldSpec("source", "select", column="source", writable=False,
                                options=("human", "derived", "import", "sync", "ai")),
            "is_derived": FieldSpec("is_derived", "boolean", column="is_derived",
                                    writable=False),
        }),
    ))

    register(EntitySpec(
        name="pipeline", table="core.pipelines", label="Pipelines",
        label_field="name", admin_only=True, supports_custom_fields=False,
        fields=_spine({
            "name": FieldSpec("name", "text", column="name", required=True),
            "stages": FieldSpec("stages", "jsonb", column="stages", filterable=False,
                                sortable=False),
        }),
    ))

    register(EntitySpec(
        name="deal", table="core.deals", label="Deals",
        label_field="name", searchable=("name",),
        fields=_spine({
            "name": FieldSpec("name", "text", column="name", required=True),
            "pipeline_id": FieldSpec("pipeline_id", "uuid", column="pipeline_id"),
            "stage": FieldSpec("stage", "text", column="stage"),
            "amount": FieldSpec("amount", "currency", column="amount"),
            "currency": FieldSpec("currency", "text", column="currency"),
            "expected_close_on": FieldSpec("expected_close_on", "date",
                                           column="expected_close_on"),
            "status": FieldSpec("status", "select", column="status",
                                options=("open", "won", "lost")),
            "last_activity_at": FieldSpec("last_activity_at", "datetime",
                                          column="last_activity_at", writable=False),
        }),
    ))

    register(EntitySpec(
        name="task", table="core.tasks", label="Tasks",
        label_field="title", searchable=("title",),
        default_sort=(("due_on", "asc"), ("updated_at", "desc")),
        fields=_spine({
            "title": FieldSpec("title", "text", column="title", required=True),
            "body": FieldSpec("body", "text", column="body"),
            "due_on": FieldSpec("due_on", "date", column="due_on"),
            "status": FieldSpec("status", "select", column="status",
                                options=("open", "done", "cancelled")),
            "subject_type": FieldSpec("subject_type", "text", column="subject_type"),
            "subject_id": FieldSpec("subject_id", "uuid", column="subject_id"),
        }),
    ))

    register(EntitySpec(
        name="note", table="core.notes", label="Notes",
        label_field="body", searchable=("body",),
        fields=_spine({
            "body": FieldSpec("body", "text", column="body", required=True),
            "subject_type": FieldSpec("subject_type", "text", column="subject_type"),
            "subject_id": FieldSpec("subject_id", "uuid", column="subject_id"),
        }),
    ))

    # Saved views and custom fields are registered entities too, not bespoke
    # routers. R4 says a new entity is a registry entry -- that applies to the
    # platform's own configuration objects, or the first hand-written CRUD in
    # the codebase is one we wrote ourselves.
    register(EntitySpec(
        name="saved_view", table="core.saved_views", label="Views",
        label_field="name", supports_custom_fields=False,
        fields=_spine({
            "entity": FieldSpec("entity", "text", column="entity", required=True),
            "name": FieldSpec("name", "text", column="name", required=True),
            "kind": FieldSpec("kind", "select", column="kind",
                              options=("table", "board", "calendar")),
            "filters": FieldSpec("filters", "jsonb", column="filters",
                                 filterable=False, sortable=False),
            "sort": FieldSpec("sort", "jsonb", column="sort",
                              filterable=False, sortable=False),
            "columns": FieldSpec("columns", "jsonb", column="columns",
                                 filterable=False, sortable=False),
            "group_by": FieldSpec("group_by", "text", column="group_by"),
            "is_shared": FieldSpec("is_shared", "boolean", column="is_shared"),
        }),
    ))

    register(EntitySpec(
        name="custom_field", table="core.custom_fields", label="Custom fields",
        label_field="label", admin_only=True, supports_custom_fields=False,
        fields=_spine({
            "entity": FieldSpec("entity", "text", column="entity", required=True),
            "key": FieldSpec("key", "text", column="key", required=True),
            "label": FieldSpec("label", "text", column="label"),
            "kind": FieldSpec("kind", "select", column="kind", options=KINDS),
            "options": FieldSpec("options", "jsonb", column="options",
                                 filterable=False, sortable=False),
            "indexed": FieldSpec("indexed", "boolean", column="indexed"),
            "index_state": FieldSpec("index_state", "text", column="index_state",
                                     writable=False),
            "index_error": FieldSpec("index_error", "text", column="index_error",
                                     writable=False),
        }),
    ))

    _register_core_roles()


def _register_core_roles() -> None:
    """Domain-neutral relationship roles. `modules/funds` adds `lp_in`,
    `portfolio_of`, `co_investor_in` and the rest through register_role()."""
    register_role(AssociationRole(
        "works_at", ("person",), ("organization",), inverse_label="employs"))
    register_role(AssociationRole(
        "board_member_of", ("person",), ("organization",), inverse_label="board members"))
    register_role(AssociationRole(
        "advisor_to", ("person",), ("organization", "person"), inverse_label="advisors"))
    register_role(AssociationRole(
        "introduced_by", ("person", "organization"), ("person",),
        inverse_label="introductions"))
    register_role(AssociationRole(
        "owns", ("organization", "person"), ("organization",), inverse_label="owned by"))
    register_role(AssociationRole(
        "vendor_to", ("organization",), ("organization",), inverse_label="vendors"))

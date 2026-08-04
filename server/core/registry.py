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
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional

if TYPE_CHECKING:
    # Type-only: registry.py stays a foundational module with no runtime
    # dependency on permissions.py, testable standalone with no database and no
    # peer imports, exactly like query.py's compiler. Only the type checker
    # needs the real class.
    from server.core.permissions import Principal

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
    # Human label for this field ("" -> the frontend derives one from `name`,
    # e.g. "fund_id" -> "fund id" -- every current UI does this in four
    # separate places; this is the one place a real label lives instead).
    label: str = ""
    # The registered entity this field's value points at, when `kind == "uuid"`
    # and the target is fixed (e.g. `commitment.fund_id` always references
    # `fund`). At most one of `references`/`references_type_field` is set.
    references: Optional[str] = None
    # For a polymorphic reference (e.g. `task.subject_id`, whose target varies
    # per row), the name of the sibling FieldSpec that holds the target entity
    # name for that row (e.g. "subject_type"). At most one of
    # `references`/`references_type_field` is set.
    references_type_field: Optional[str] = None
    # Visibility level a principal needs to write this field. `owner_id`
    # declares "team": otherwise a user at `own` level can reassign a record
    # away from themselves (losing it) or to themselves (stealing it).
    write_level: str = "own"
    # Visibility level a principal needs, relative to the ROW's owner_id, to
    # READ this field's real value -- None means "governed by the entity's
    # ordinary read_level/field_masks only," the behavior every field had
    # before this existed. Set to "own" for a field like `interaction.body`
    # (safety rule 10): every other column on that row is org-wide, but this
    # one is visible only to the row's owner (or an admin), regardless of
    # the requesting principal's overall read_level on the entity. Checked
    # per-row in repository.py's `_finalize_read()`, not compiled into the
    # WHERE clause -- unlike write_level's entity-wide check, this depends
    # on which specific row was fetched.
    read_level: Optional[str] = None
    # A pure function of (the fetched row, the entity's context -- see
    # EntitySpec.context_builder) -> the value to inject under this
    # field's name. No `column`/`custom_key`, and never `writable` (a
    # computed field has nothing to write to). M7's deal-rotting flag is
    # the first user: it's derived from `last_activity_at` and a per-org
    # threshold, not a stored/recomputed column (server/core/registry.py's
    # `deal` entity block is the only place that knows what "rotting"
    # means -- this field itself is domain-neutral plumbing, R6-clean).
    compute: Optional[Callable[[dict[str, Any], dict[str, Any]], Any]] = None
    # A pure function of the submitted value -> its canonical form, or None
    # if the value is not valid for this field. Applied by repository.py's
    # write path to every present, truthy field that declares one, BEFORE
    # the row is written -- the generic version of what `_derive_normalized`
    # used to hardcode per core entity name. This is what lets a module
    # declare `normalize=identity.normalize_country` on its own field and
    # get exactly the same validate-and-canonicalize behavior organizations/
    # persons get, without repository.py (core) ever learning the module's
    # entity name (R6) or a validator -- which can only reject, never
    # rewrite a column -- standing in as a worse substitute (R1).
    normalize: Optional[Callable[[Any], Optional[Any]]] = None

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
    # Built ONCE per list()/get() call (never per row) and handed to
    # every field's `compute()` -- so a computed field that needs
    # something beyond the row itself (M7's rotting flag needs a
    # per-org settings lookup) does that lookup a single time, not once
    # per returned record. None for every entity with no computed
    # fields, which is most of them.
    context_builder: Optional[Callable[["Principal"], dict[str, Any]]] = None
    # Where this entity appears in the sidebar. "" derives a default at
    # registration-adjacent read time (verify() enforces the closed
    # vocabulary; Shell.tsx/the /records endpoint derive "" -> "settings" for
    # an admin_only entity, else "primary"): "primary" (top-level nav item),
    # "settings" (lives under the Settings shell), "none" (reached only from
    # a parent record -- a pure join/child row like `pathway_vehicle` or
    # `contact_channel`, never its own nav entry).
    nav: str = ""
    # Sidebar section label ("" = the unlabeled top block). A module names
    # its own group ("Investing", "Investors") in its own manifest -- core
    # never learns a vertical's grouping (R6).
    nav_group: str = ""
    nav_order: int = 100
    # Curated default table columns, in order. () means "derive one" (today's
    # behavior: every field but id/owner_id/created_at) -- set explicitly for
    # any entity whose full field list is the wrong table (a jsonb blob, a
    # pile of uuid FKs with no label yet).
    list_columns: tuple[str, ...] = ()

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

    `hierarchical` roles form a rollup dimension (child = `from_id`, parent =
    `to_id`) walked by `server/core/hierarchy.py`'s recursive CTE and guarded
    against cycles by `associations.associate()` at write time -- see that
    module's docstring for why both the cycle check and its depth cap ship
    together.
    """
    name: str
    from_types: tuple[str, ...]
    to_types: tuple[str, ...]
    inverse_label: str = ""
    symmetric: bool = False
    hierarchical: bool = False
    module: str = "core"
    # Human label for the outbound direction ("" -> the frontend falls back
    # to `name.replace("_", " ")`, today's only behavior). `inverse_label`
    # above is already the inbound-direction label; this is its counterpart.
    label: str = ""
    # Which section of the related-records panel this role's edges render
    # under (e.g. "Governance & signing", "Investing"). "" falls back to the
    # role's own `module` name, so an unsectioned role still groups instead
    # of scattering into one block per role.
    group: str = ""
    group_order: int = 100


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


def role_presentation(role_spec: AssociationRole, direction: str) -> dict[str, Any]:
    """The human label/group for one direction of a role -- 'out' is the
    initiating side (`role_spec.label`), 'in' is the receiving side
    (`role_spec.inverse_label`), matching `core.association_edges`' own
    direction vocabulary (server/db/schema.py's VIEWS).

    The one place both `server/api/records.py`'s `entity_roles()` (the "+
    Link" role picker) and `server/core/associations.py`'s `related_blocks()`
    (the related-records panel) derive this, so a role's presentation cannot
    drift between the two the way it did before this existed: `related_blocks()`
    used to key its 'out'-direction blocks on the raw role name (e.g. `lp_in`)
    while `entity_roles()` already showed the humanized `label` ("LP in") in
    the picker that creates the very same edge.
    """
    if direction == "in":
        label = role_spec.inverse_label or role_spec.name.replace("_", " ")
    elif direction == "out":
        label = role_spec.label or role_spec.name.replace("_", " ")
    else:
        raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
    return {
        "label": label,
        "group": role_spec.group or role_spec.module,
        "group_order": role_spec.group_order,
    }


# ── Write-time validators ────────────────────────────────────────────────────
#
# The gate the event bus (server/core/events.py) cannot give you. Events are a
# transactional outbox whose subscribers run AFTER commit and cannot block a
# write -- correct for "notify something," wrong for "a commitment must not
# close without an executed subscription document." That needs to run INSIDE
# the write, before it commits, and be able to raise and abort it.
#
# A validator is called synchronously, inside repository.create/update/delete's
# own transaction, after the row has been written (so `after` is the real,
# fully-resolved row -- defaults, normalization and all, not a hand-rolled
# guess at what the row will look like) but before that transaction commits.
# Raising propagates out of repository's `with pool.transaction(...)` block,
# which rolls back and re-raises -- the same "raise, never no-op" discipline
# every other gate in this platform follows. There is no way to register a
# validator that silently swallows a failure; the callable either returns
# (allow) or raises (abort), nothing in between.
#
# Modules register validators through this exact function, the same way they
# register entities and roles, so `modules/investor_portal` can enforce a rule
# about `commitment` (a `modules/funds` entity) without `server/core/` or
# `modules/funds` ever knowing that rule exists (R6).


@dataclass(frozen=True)
class ValidationContext:
    """What a validator sees. `before`/`after` are plain dicts, already merged
    with defaults and normalization by the repository -- a validator never
    re-derives what the write does, only judges the result.

    `before` is None on create. `after` is None on delete. Both are set on
    update. `record_id` is always set except on a create the validator itself
    rejects before an id would otherwise matter -- it is always populated in
    practice, since the id is minted before the row is written.
    """
    principal: "Principal"
    entity: str
    action: str  # 'create' | 'update' | 'delete'
    record_id: Optional[str]
    before: Optional[dict[str, Any]]
    after: Optional[dict[str, Any]]


Validator = Callable[[ValidationContext], None]


@dataclass(frozen=True)
class _RegisteredValidator:
    entity: str
    fn: Validator
    actions: tuple[str, ...]
    order: int
    module: str


_VALIDATORS: list[_RegisteredValidator] = []


def register_validator(
    entity_name: str,
    fn: Validator,
    *,
    actions: tuple[str, ...] = ("create", "update", "delete"),
    order: int = 100,
    module: str = "core",
) -> None:
    """Register a write-time rule for `entity_name`. `fn` raises to abort the
    write (any exception; `repository.ValidationError` is conventional but not
    required) or returns normally to allow it.

    Multiple validators on the same entity all run, in `order` then
    registration order, for every action in their declared `actions` tuple --
    a validator scoped to `actions=("update",)` never fires on create or
    delete, so a rule about closing a commitment cannot accidentally block
    creating one.

    Idempotent like `register()`/`register_role()`: installing the same
    module twice (every real app startup and every test that spins up a
    fresh `TestClient` both call `modules.install_enabled_modules()`) must
    re-declare the same rule, not accumulate a second copy of it that runs
    twice per write.
    """
    for existing in _VALIDATORS:
        if existing.entity == entity_name and existing.fn is fn and existing.module == module:
            return
    _VALIDATORS.append(_RegisteredValidator(entity_name, fn, actions, order, module))
    _VALIDATORS.sort(key=lambda v: v.order)


def validators_for(entity_name: str, action: str) -> list[Validator]:
    return [v.fn for v in _VALIDATORS if v.entity == entity_name and action in v.actions]


def reset_validators() -> None:
    """Drop every registered validator. Tests only."""
    _VALIDATORS.clear()


def validator_snapshot() -> list[_RegisteredValidator]:
    """An opaque snapshot of the current validator list, for a test that adds
    one temporarily and must remove exactly what it added.

    A blanket `reset_validators()` is wrong for this purpose the moment a
    module registers a validator meant to survive for the whole test session
    (e.g. `modules/investor_portal`'s commitment-closing gate, installed once
    at session scope) -- resetting after every test would delete it
    permanently, since nothing re-installs modules per test.
    """
    return list(_VALIDATORS)


def restore_validators(snapshot: list[_RegisteredValidator]) -> None:
    _VALIDATORS[:] = snapshot


# ── Org-creation seeds ───────────────────────────────────────────────────────
#
# A handful of module-owned reference tables (e.g. investor_portal's
# investor_categories) need a fixed set of default rows the moment an org
# exists, not left for an admin to type in by hand. `users.create_org()` is
# the one place an org is born; this is the seam that lets a module contribute
# to that moment without `users.py` importing the module directly (R6) --
# same shape as `register_validator`.

@dataclass(frozen=True)
class _RegisteredOrgSeed:
    fn: Callable[[str], None]
    module: str


_ORG_SEEDS: list[_RegisteredOrgSeed] = []


def register_org_seed(fn: Callable[[str], None], *, module: str = "core") -> None:
    """Register a callable invoked once with a new org's id, right after
    `create_org()`'s own transaction commits. `fn` should write through
    `repository` with `permissions.system_principal(org_id, ...)`, exactly
    like an event subscriber, not open its own cursor.

    Idempotent like `register_validator`: re-declaring the same seed on a
    second `install()` call does not duplicate it.
    """
    for existing in _ORG_SEEDS:
        if existing.fn is fn and existing.module == module:
            return
    _ORG_SEEDS.append(_RegisteredOrgSeed(fn, module))


def org_seeds() -> list[Callable[[str], None]]:
    return [s.fn for s in _ORG_SEEDS]


def reset_org_seeds() -> None:
    """Drop every registered org seed. Tests only."""
    _ORG_SEEDS.clear()


def reset() -> None:
    """Drop every registration. Tests only."""
    _ENTITIES.clear()
    _ROLES.clear()
    _VALIDATORS.clear()
    _ORG_SEEDS.clear()


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

_NAV_VALUES = ("", "primary", "settings", "none")


def verify(tables: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Check every registration against the real schema. Empty list means sound.

    Called from the app lifespan beside schema.verify_rls(). Catches the
    owner_id case described in the module docstring, plus a field declared over
    a column that does not exist -- which would otherwise surface as a SQL error
    on whichever request first selected it.
    """
    problems: list[dict[str, Any]] = []
    known = set(entities())

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
            # A `compute` field has no `column`/`custom_key`, so leaving it
            # filterable/sortable (the dataclass default) makes the filter
            # compiler's `_lhs()` fall through to the custom-field branch and
            # emit `core.jsonb_text(t.custom, NULL)` -- silently wrong SQL
            # against a column that doesn't hold the value, not an error.
            # `deal.rotting` gets this right by hand; enforce it for every
            # future compute field instead of trusting each caller to notice.
            if fspec.compute is not None and (fspec.filterable or fspec.sortable):
                problems.append({"entity": spec.name, "problem": "computed_field_filterable",
                                 "field": name})
            if fspec.references is not None and fspec.references_type_field is not None:
                problems.append({"entity": spec.name, "problem": "field_has_both_reference_kinds",
                                 "field": name})
            if fspec.references is not None and fspec.references not in known:
                problems.append({"entity": spec.name, "problem": "reference_unknown_entity",
                                 "field": name, "references": fspec.references})
            if fspec.references_type_field is not None and fspec.references_type_field not in spec.fields:
                problems.append({"entity": spec.name, "problem": "reference_type_field_missing",
                                 "field": name, "references_type_field": fspec.references_type_field})
        for field_name, direction in spec.default_sort:
            if field_name not in spec.fields:
                problems.append({"entity": spec.name, "problem": "sort_field_missing",
                                 "field": field_name})
            if direction not in ("asc", "desc"):
                problems.append({"entity": spec.name, "problem": "bad_sort_direction",
                                 "direction": direction})
        if spec.label_field not in spec.fields:
            problems.append({"entity": spec.name, "problem": "label_field_missing",
                             "field": spec.label_field})
        elif spec.fields[spec.label_field].compute is not None and not spec.searchable:
            # A computed label_field is unfilterable (enforced above), so
            # EntityListPage's own fallback -- filter on `searchable`, or on
            # `[label_field]` when `searchable` is empty (EntityListPage.tsx:33)
            # -- would try to filter directly on the computed field. Requiring
            # a real `searchable` whenever label_field is computed closes that.
            problems.append({"entity": spec.name, "problem": "computed_label_field_needs_searchable"})
        for col in spec.list_columns:
            if col not in spec.fields:
                problems.append({"entity": spec.name, "problem": "list_column_missing",
                                 "field": col})
        if spec.nav not in _NAV_VALUES:
            problems.append({"entity": spec.name, "problem": "bad_nav", "nav": spec.nav})

    for role_spec in roles():
        for side in role_spec.from_types + role_spec.to_types:
            if side not in known:
                problems.append({"role": role_spec.name, "problem": "unknown_entity",
                                 "entity": side})

    for validator in _VALIDATORS:
        if validator.entity not in known:
            problems.append({
                "validator_module": validator.module,
                "problem": "unknown_entity", "entity": validator.entity,
            })
    return problems


# ── Core entities ────────────────────────────────────────────────────────────

def spine(extra: dict[str, FieldSpec]) -> dict[str, FieldSpec]:
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


# ── M7 deal rotting -- a computed field, no job, no stored column ───────────
# "Rotting is a pure function of now() - last_activity_at against a
# configurable threshold, computed at read time... simpler and correct:
# don't build a mechanism when a formula already suffices." The formula
# itself lives here, next to the rest of `deal`'s own field declarations
# -- FieldSpec.compute/EntitySpec.context_builder (registry.py/repository.py)
# are domain-neutral plumbing that knows nothing about deals; this is the
# one place that does.

_DEFAULT_ROTTING_THRESHOLD_DAYS = 14


def _deal_rotting_context(principal: "Principal") -> dict[str, Any]:
    # Local import: registry.py stays a foundational module with no
    # runtime dependency on peer core modules (see the module docstring)
    # -- deferred to call time, exactly like server/providers/calendar.py's
    # per-branch lazy imports.
    from server.core import settings as core_settings

    pipeline = core_settings.get_settings(principal.org_id, "pipeline")
    return {
        "rotting_threshold_days": pipeline.get(
            "rotting_threshold_days", _DEFAULT_ROTTING_THRESHOLD_DAYS
        ),
    }


def _deal_rotting(record: dict[str, Any], context: dict[str, Any]) -> bool:
    """True when this deal is still open and its last recorded activity
    (server/core/deal_activity.py keeps `last_activity_at` current) is
    older than the org's configured threshold. A deal with no recorded
    activity at all, or one that's already closed (won/lost), is never
    "rotting" -- fails safe to `False` rather than flagging every deal
    a fresh install has never touched (safety rule 8's same
    null-never-auto-matches shape, applied to a derived flag instead of
    a category)."""
    if record.get("status") != "open":
        return False
    last_activity = record.get("last_activity_at")
    if last_activity is None:
        return False
    if isinstance(last_activity, str):
        last_activity = datetime.fromisoformat(last_activity)
    threshold_days = context.get("rotting_threshold_days", _DEFAULT_ROTTING_THRESHOLD_DAYS)
    return datetime.now(timezone.utc) - last_activity > timedelta(days=threshold_days)


# `interaction.display_label` -- a computed title, replacing the raw
# `subject_hash` a design audit found rendering literally as a hash in every
# table/detail title. No context_builder needed (nothing per-org to look up).
def _interaction_display_label(record: dict[str, Any], context: dict[str, Any]) -> str:
    kind = str(record.get("kind") or "interaction").capitalize()
    occurred = record.get("occurred_at")
    if isinstance(occurred, str):
        try:
            occurred = datetime.fromisoformat(occurred)
        except ValueError:
            occurred = None
    if isinstance(occurred, datetime):
        return f"{kind} — {occurred.strftime('%b %d, %Y')}"
    return kind


def register_core_entities() -> None:
    """Declare the domain-neutral core. Idempotent."""
    # Local import: registry.py stays a foundational module with no runtime
    # dependency on peer core modules (see the module docstring) -- deferred
    # to call time, matching every other cross-module import in this file.
    from server.core import identity

    register(EntitySpec(
        name="organization", table="core.organizations", label="Companies",
        label_field="name", searchable=("name", "domain"),
        nav_group="Overview", nav_order=10,
        list_columns=("name", "domain", "domicile_country", "is_internal"),
        fields=spine({
            "name": FieldSpec("name", "text", column="name", required=True),
            "domain": FieldSpec("domain", "text", column="domain"),
            "description": FieldSpec("description", "text", column="description"),
            "is_internal": FieldSpec("is_internal", "boolean", column="is_internal"),
            "source": FieldSpec("source", "select", column="source", writable=False,
                                options=("human", "derived", "import", "sync", "ai")),
            "is_derived": FieldSpec("is_derived", "boolean", column="is_derived",
                                    writable=False),
            "domicile_country": FieldSpec("domicile_country", "text",
                                          column="domicile_country",
                                          normalize=identity.normalize_country),
        }),
    ))

    register(EntitySpec(
        name="person", table="core.persons", label="People",
        label_field="full_name", searchable=("full_name", "primary_email"),
        nav_group="Overview", nav_order=20,
        list_columns=("full_name", "title", "primary_email", "tax_residence_country"),
        fields=spine({
            "full_name": FieldSpec("full_name", "text", column="full_name", required=True),
            "title": FieldSpec("title", "text", column="title"),
            "primary_email": FieldSpec("primary_email", "email", column="primary_email",
                                       normalize=identity.normalize_email),
            "description": FieldSpec("description", "text", column="description"),
            "source": FieldSpec("source", "select", column="source", writable=False,
                                options=("human", "derived", "import", "sync", "ai")),
            "is_derived": FieldSpec("is_derived", "boolean", column="is_derived",
                                    writable=False),
            "auto_accept": FieldSpec("auto_accept", "boolean", column="auto_accept"),
            "tax_residence_country": FieldSpec("tax_residence_country", "text",
                                               column="tax_residence_country",
                                               normalize=identity.normalize_country),
            "citizenship_country": FieldSpec("citizenship_country", "text",
                                             normalize=identity.normalize_country,
                                             column="citizenship_country"),
        }),
    ))

    register(EntitySpec(
        name="pipeline", table="core.pipelines", label="Pipelines",
        label_field="name", admin_only=True, supports_custom_fields=False,
        fields=spine({
            "name": FieldSpec("name", "text", column="name", required=True),
            "stages": FieldSpec("stages", "jsonb", column="stages", filterable=False,
                                sortable=False),
        }),
    ))

    register(EntitySpec(
        name="trusted_sender", table="core.trusted_senders", label="Trusted Senders",
        label_field="pattern", admin_only=True, supports_custom_fields=False,
        fields=spine({
            "pattern": FieldSpec("pattern", "text", column="pattern", required=True),
            "label": FieldSpec("label", "text", column="label"),
            "note": FieldSpec("note", "text", column="note"),
        }),
    ))

    register(EntitySpec(
        name="deal", table="core.deals", label="Deals",
        label_field="name", searchable=("name",),
        context_builder=_deal_rotting_context,
        nav_group="Overview", nav_order=30,
        list_columns=("name", "stage", "amount", "status", "rotting"),
        fields=spine({
            "name": FieldSpec("name", "text", column="name", required=True),
            "pipeline_id": FieldSpec("pipeline_id", "uuid", column="pipeline_id",
                                     label="Pipeline", references="pipeline"),
            "stage": FieldSpec("stage", "text", column="stage"),
            "amount": FieldSpec("amount", "currency", column="amount"),
            "currency": FieldSpec("currency", "text", column="currency"),
            "expected_close_on": FieldSpec("expected_close_on", "date",
                                           column="expected_close_on"),
            "status": FieldSpec("status", "select", column="status",
                                options=("open", "won", "lost")),
            "last_activity_at": FieldSpec("last_activity_at", "datetime",
                                          column="last_activity_at", writable=False),
            "rotting": FieldSpec("rotting", "boolean", filterable=False, sortable=False,
                                 writable=False, compute=_deal_rotting),
        }),
    ))

    register(EntitySpec(
        name="task", table="core.tasks", label="Tasks",
        label_field="title", searchable=("title",),
        default_sort=(("due_on", "asc"), ("updated_at", "desc")),
        nav_group="Overview", nav_order=40,
        list_columns=("title", "status", "due_on", "subject_type"),
        fields=spine({
            "title": FieldSpec("title", "text", column="title", required=True),
            "body": FieldSpec("body", "text", column="body"),
            "due_on": FieldSpec("due_on", "date", column="due_on"),
            "status": FieldSpec("status", "select", column="status",
                                options=("open", "done", "cancelled")),
            "subject_type": FieldSpec("subject_type", "text", column="subject_type"),
            "subject_id": FieldSpec("subject_id", "uuid", column="subject_id",
                                    references_type_field="subject_type"),
        }),
    ))

    register(EntitySpec(
        name="note", table="core.notes", label="Notes",
        label_field="body", searchable=("body",),
        nav_group="Activity", nav_order=30,
        list_columns=("body", "subject_type", "updated_at"),
        fields=spine({
            "body": FieldSpec("body", "text", column="body", required=True),
            "subject_type": FieldSpec("subject_type", "text", column="subject_type"),
            "subject_id": FieldSpec("subject_id", "uuid", column="subject_id",
                                    references_type_field="subject_type"),
        }),
    ))

    # Generic, subject-polymorphic like tasks/notes -- documents are core, not
    # vertical (docs/INVESTOR-PORTAL.md). "Subscription agreement" is meaning a
    # module attaches via `kind`; this table has no idea what a commitment is.
    # `provider`/`provider_envelope_id` are free text so a new esign provider
    # (R3's sixth axis) never needs a schema change to be recorded here.
    register(EntitySpec(
        name="document", table="core.documents", label="Documents",
        label_field="filename", searchable=("filename", "kind"),
        nav_group="Activity", nav_order=20,
        list_columns=("filename", "kind", "status", "valid_until"),
        fields=spine({
            "subject_type": FieldSpec("subject_type", "text", column="subject_type"),
            "subject_id": FieldSpec("subject_id", "uuid", column="subject_id",
                                    references_type_field="subject_type"),
            "kind": FieldSpec("kind", "text", column="kind"),
            "filename": FieldSpec("filename", "text", column="filename"),
            "storage_key": FieldSpec("storage_key", "text", column="storage_key",
                                     writable=False),
            "mime": FieldSpec("mime", "text", column="mime"),
            "bytes": FieldSpec("bytes", "number", column="bytes", writable=False),
            "provider": FieldSpec("provider", "text", column="provider"),
            "provider_envelope_id": FieldSpec("provider_envelope_id", "text",
                                              column="provider_envelope_id",
                                              writable=False),
            "status": FieldSpec(
                "status", "select", column="status",
                options=("draft", "sent_for_signature", "partially_signed",
                         "executed", "void", "expired"),
            ),
            "valid_from": FieldSpec("valid_from", "date", column="valid_from"),
            "valid_until": FieldSpec("valid_until", "date", column="valid_until"),
        }),
    ))

    # M2's primitive (docs/DESIGN.md): the CRM is a derived view over this
    # log. `body` is the one field safety rule 10 governs -- every other
    # column is org-wide, `body` is visible only to the row's owner (or an
    # admin), via `read_level="own"` rather than the entity's ordinary
    # read_level. `owner_id` (the spine field every entity carries) IS "whose
    # mailbox observed this" here. Not marked required=True on the FieldSpec:
    # repository.create() checks `required` BEFORE its owner_id.setdefault()
    # runs, so that would reject the ordinary case of a human letting their
    # own id default in. The DB's NOT NULL is the actual guarantee -- an
    # explicit owner_id is only needed from a future system-principal writer
    # (a sync job), which gets a clear constraint violation if it omits one.
    register(EntitySpec(
        name="interaction", table="core.interactions", label="Interactions",
        label_field="display_label", searchable=("thread_id", "from_channel"),
        default_sort=(("occurred_at", "desc"),),
        nav_group="Activity", nav_order=10,
        list_columns=("display_label", "direction", "occurred_at", "thread_id"),
        fields=spine({
            "owner_id": FieldSpec("owner_id", "uuid", column="owner_id",
                                  write_level="team"),
            "account_id": FieldSpec("account_id", "uuid", column="account_id"),
            "occurred_at": FieldSpec("occurred_at", "datetime", column="occurred_at"),
            "kind": FieldSpec("kind", "select", column="kind",
                              options=("email", "call", "meeting", "sms", "chat", "other")),
            "direction": FieldSpec("direction", "select", column="direction",
                                   options=("inbound", "outbound", "internal")),
            "thread_id": FieldSpec("thread_id", "text", column="thread_id"),
            "from_channel": FieldSpec("from_channel", "text", column="from_channel"),
            "to_channels": FieldSpec("to_channels", "jsonb", column="to_channels",
                                     filterable=False, sortable=False),
            "subject_hash": FieldSpec("subject_hash", "text", column="subject_hash"),
            "body": FieldSpec("body", "text", column="body", read_level="own"),
            "external_id": FieldSpec("external_id", "text", column="external_id"),
            "source": FieldSpec("source", "select", column="source", writable=False,
                                options=("human", "sync", "ai")),
            # Computed, not stored -- `subject_hash` is a dedup key, not a
            # title (raw UUID/hash, per the design audit that motivated this
            # field). "Email — Aug 4, 2026" reads; a hash doesn't. Same
            # compute/context_builder mechanism as `deal.rotting`, and the
            # `filterable=False, sortable=False` + non-empty `searchable`
            # pairing verify() now enforces for any computed label_field.
            "display_label": FieldSpec("display_label", "text", filterable=False,
                                       sortable=False, writable=False,
                                       compute=_interaction_display_label),
        }),
    ))

    # Identity resolution -- unique on (kind, value_normalized), see
    # server/core/identity.py's module docstring. `value_normalized` is
    # writable through the generic path in principle, but every real writer
    # goes through `identity.normalize_required()` first (server/core/
    # derivation.py); nothing here enforces that at the field level, the same
    # trust boundary every other normalized column in this platform has.
    register(EntitySpec(
        name="contact_channel", table="core.contact_channels",
        label="Contact channels", label_field="value_normalized",
        searchable=("value_normalized", "value_raw"),
        # A person's own email/phone list, not a nav-worthy object in its own
        # right -- reached from the person record, per the design audit's
        # "pure join/child row" call.
        nav="none",
        fields=spine({
            "person_id": FieldSpec("person_id", "uuid", column="person_id", required=True,
                                   label="Person", references="person"),
            "kind": FieldSpec("kind", "select", column="kind",
                              options=("email", "phone", "signal", "telegram", "handle")),
            "value_normalized": FieldSpec("value_normalized", "text",
                                          column="value_normalized", required=True),
            "value_raw": FieldSpec("value_raw", "text", column="value_raw"),
            "is_primary": FieldSpec("is_primary", "boolean", column="is_primary"),
        }),
    ))

    register(EntitySpec(
        name="metric_fact", table="core.metric_facts", label="Metrics",
        label_field="metric_key", searchable=("metric_key",),
        default_sort=(("period_start", "desc"),),
        nav_group="Activity", nav_order=40,
        list_columns=("metric_key", "value_numeric", "period_start", "subject_type"),
        fields=spine({
            "subject_type": FieldSpec("subject_type", "text", column="subject_type"),
            "subject_id": FieldSpec("subject_id", "uuid", column="subject_id",
                                    references_type_field="subject_type"),
            "period_start": FieldSpec("period_start", "date", column="period_start"),
            "period_end": FieldSpec("period_end", "date", column="period_end"),
            "metric_key": FieldSpec("metric_key", "text", column="metric_key",
                                    required=True),
            "value_numeric": FieldSpec("value_numeric", "number", column="value_numeric"),
            "value_text": FieldSpec("value_text", "text", column="value_text"),
            "currency": FieldSpec("currency", "text", column="currency"),
            "source": FieldSpec("source", "select", column="source",
                                options=("human", "sync", "ai")),
            "confidence": FieldSpec("confidence", "number", column="confidence"),
            "document_id": FieldSpec("document_id", "uuid", column="document_id",
                                     label="Document", references="document"),
        }),
    ))

    # server/core/proposals.py's "one approval queue." Every field is
    # writable=False through the generic REST/repository path -- the
    # generic UI browses proposals for free (R4), but a real decision only
    # ever happens through proposals.approve()/decline()/auto_approve(),
    # safety rule 3's single execution choke point.
    register(EntitySpec(
        name="proposed_change", table="core.proposed_changes", label="Proposals",
        label_field="kind", supports_custom_fields=False,
        default_sort=(("created_at", "desc"),),
        # The real approve/decline flow is the dedicated Scheduling
        # Suggestions page (server/api/proposals.py), not this generic list
        # -- every field here is writable=False, so a generic entry is a
        # dead end.
        nav="none",
        fields=spine({
            # spine()'s default owner_id is writable at "team" level, but
            # every field here must be writable=False (server/api/proposals.py's
            # own docstring says so) -- proposals.py's CAS functions are the
            # only mutation path, and create() is the only place owner_id
            # is ever set.
            "owner_id": FieldSpec("owner_id", "uuid", column="owner_id", writable=False),
            "subject_type": FieldSpec("subject_type", "text", column="subject_type",
                                      writable=False),
            "subject_id": FieldSpec("subject_id", "uuid", column="subject_id",
                                    writable=False, references_type_field="subject_type"),
            "kind": FieldSpec("kind", "text", column="kind", writable=False),
            "payload": FieldSpec("payload", "jsonb", column="payload",
                                 filterable=False, sortable=False, writable=False),
            "confidence": FieldSpec("confidence", "number", column="confidence",
                                    writable=False),
            "category": FieldSpec("category", "text", column="category", writable=False),
            "status": FieldSpec("status", "select", column="status", writable=False,
                                options=("pending", "approved", "declined", "auto_approved")),
            "decided_by": FieldSpec("decided_by", "text", column="decided_by",
                                    writable=False),
            "decided_at": FieldSpec("decided_at", "datetime", column="decided_at",
                                    writable=False),
            "notification_message_id": FieldSpec(
                "notification_message_id", "text", column="notification_message_id",
                writable=False,
            ),
        }),
    ))

    # Saved views and custom fields are registered entities too, not bespoke
    # routers. R4 says a new entity is a registry entry -- that applies to the
    # platform's own configuration objects, or the first hand-written CRUD in
    # the codebase is one we wrote ourselves.
    register(EntitySpec(
        name="saved_view", table="core.saved_views", label="Views",
        label_field="name", supports_custom_fields=False,
        # The nav mechanism itself (nested under each object per
        # DESIGN.md:222, Phase 7) -- browsing it as its own top-level list
        # would be circular.
        nav="none",
        fields=spine({
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
        fields=spine({
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
        "works_at", ("person",), ("organization",), inverse_label="employs",
        label="Works at", group="People", group_order=30))
    register_role(AssociationRole(
        "board_member_of", ("person",), ("organization",), inverse_label="board members",
        label="Board member of", group="People", group_order=30))
    register_role(AssociationRole(
        "advisor_to", ("person",), ("organization", "person"), inverse_label="advisors",
        label="Advisor to", group="People", group_order=30))
    register_role(AssociationRole(
        "introduced_by", ("person", "organization"), ("person",),
        inverse_label="introductions",
        label="Introduced by", group="People", group_order=30))
    register_role(AssociationRole(
        "vendor_to", ("organization",), ("organization",), inverse_label="vendors",
        label="Vendor to", group="Commercial", group_order=60))
    # Legal/corporate ownership -- the Ultimate Parent Company hierarchy.
    # `from` is the owned org (the child), `to` is the owner (an org or a
    # person -- a GP with no corporate parent is routinely owned directly by
    # a person), matching `hierarchy.py`'s child-to-parent convention and the
    # same from=subordinate/to=container direction `lp_in`/`portfolio_of`
    # already use. `owned_by` reads correctly from the child's own record;
    # `inverse_label="owns"` is what the owner's page shows. Deliberately
    # domain-neutral (every CRM wants "who owns whom") -- unlike
    # `modules/funds`' `rolls_up_to`, which is an investment-relationship
    # rollup that is explicitly NOT legal-entity-based. Same traversal
    # mechanism (server/core/hierarchy.py), two different real-world
    # concepts, hence two roles.
    register_role(AssociationRole(
        "owned_by", ("organization",), ("organization", "person"),
        inverse_label="owns", hierarchical=True,
        label="Owned by", group="Ownership", group_order=10))

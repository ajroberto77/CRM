"""The EspoCRM-shaped permission model, and THE only place it is decided.

Four things, and deliberately not a fifth:

  1. Role -> object -> action grid (create / read / edit / delete).
  2. Record visibility per action: all | team | own | none.
  3. Field masking: a field can be hidden or made read-only for a role.
  4. A hard admin / non-admin split for settings and credentials.

Multiple roles merge PERMISSIVELY -- if any role grants an action, the user has
it, and the widest visibility level wins. Absence of a scope row is deny.

## What this deliberately is not

No sharing rules, no territory hierarchies, no org-wide defaults with per-object
overrides, no manual per-record shares, no ABAC engine. The reason is not
scope-cutting: it is that those turn visibility from a predicate you can read
into a computed set backed by materialized share tables, which every query path,
export, background job and AI agent must then route through or leak. See
docs/DESIGN.md. Because the whole model lives in this file and resolves to a
SQL fragment, adding a sharing rule later is a change here, not a migration.

## The two layers

RLS in Postgres carries the ORG boundary and nothing else -- absolute, cheap,
and impossible for any code path to bypass. This module carries RECORD
visibility (own/team), because those predicates change per query and are far
easier to index and debug as SQL you can read. `visibility_predicate()` returns
that fragment; server/core/repository.py is the single choke point that injects
it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from server.db import pool

ACTIONS = ("create", "read", "edit", "delete")
LEVELS = ("none", "own", "team", "all")

# Ordered weakest -> strongest, so a permissive merge is a max().
_LEVEL_RANK = {level: index for index, level in enumerate(LEVELS)}


class PermissionDenied(PermissionError):
    """Raised, never returned as a no-op.

    A gate that silently does nothing lets the caller believe the work
    succeeded and mark it done -- a sibling project shipped exactly that bug and
    wrote real calendar events while writes were disabled. Every gate in this
    codebase raises.
    """

    def __init__(self, action: str, obj: str, detail: str = "") -> None:
        message = f"not permitted to {action} {obj}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)
        self.action = action
        self.object = obj


def widest(*levels: str) -> str:
    """The permissive merge of visibility levels."""
    best = "none"
    for level in levels:
        if _LEVEL_RANK.get(level, 0) > _LEVEL_RANK[best]:
            best = level
    return best


@dataclass(frozen=True)
class ObjectPermissions:
    """A user's merged permissions on one registered object."""
    object: str
    can_create: bool = False
    read_level: str = "none"
    edit_level: str = "none"
    delete_level: str = "none"
    # field -> ceiling. 'none' hides the field; 'read' makes it read-only.
    field_masks: dict[str, str] = field(default_factory=dict)

    def level_for(self, action: str) -> str:
        if action == "create":
            return "all" if self.can_create else "none"
        return getattr(self, f"{action}_level", "none")

    def allows(self, action: str) -> bool:
        return self.level_for(action) != "none"

    def readable_field(self, name: str) -> bool:
        return self.field_masks.get(name) != "none"

    def writable_field(self, name: str) -> bool:
        return name not in self.field_masks


@dataclass(frozen=True)
class Principal:
    """Who is acting. Built once per request and passed down; nothing below the
    API layer re-derives permissions."""
    user_id: str
    org_id: str
    is_admin: bool = False
    team_id: Optional[str] = None
    permissions: dict[str, ObjectPermissions] = field(default_factory=dict)

    def for_object(self, obj: str) -> ObjectPermissions:
        """An admin has everything. Otherwise, an object with no scope row
        anywhere is fully denied -- default deny, not default allow."""
        if self.is_admin:
            return ObjectPermissions(
                object=obj,
                can_create=True,
                read_level="all",
                edit_level="all",
                delete_level="all",
            )
        return self.permissions.get(obj, ObjectPermissions(object=obj))

    def require(self, action: str, obj: str) -> ObjectPermissions:
        """Assert and return. This is the call sites use; it raises rather than
        returning a boolean so a forgotten `if` cannot become an open door."""
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r} (known: {', '.join(ACTIONS)})")
        perms = self.for_object(obj)
        if not perms.allows(action):
            raise PermissionDenied(action, obj)
        return perms

    def require_admin(self, detail: str = "admin only") -> None:
        if not self.is_admin:
            raise PermissionDenied("administer", "settings", detail)


def load_principal(user: dict[str, Any]) -> Principal:
    """Resolve a user row into a Principal by merging every role they hold.

    One query for scopes and one for masks, both already tenant-filtered by RLS.
    Admins skip the queries entirely -- for_object() short-circuits for them, so
    loading their scopes would be work whose result is discarded.
    """
    user_id = str(user["id"])
    org_id = str(user["org_id"])
    is_admin = bool(user.get("is_admin"))
    team_id = str(user["team_id"]) if user.get("team_id") else None

    if is_admin:
        return Principal(user_id=user_id, org_id=org_id, is_admin=True, team_id=team_id)

    merged: dict[str, dict[str, Any]] = {}
    with pool.transaction(org_id=org_id, user_id=user_id, readonly=True) as cur:
        cur.execute(
            "SELECT s.object, s.can_create, s.read_level, s.edit_level, s.delete_level "
            "FROM core.role_scopes s "
            "JOIN core.user_roles ur ON ur.role_id = s.role_id "
            "WHERE ur.user_id = %s",
            (user_id,),
        )
        for row in cur.fetchall():
            obj = row["object"]
            current = merged.setdefault(
                obj,
                {
                    "can_create": False,
                    "read_level": "none",
                    "edit_level": "none",
                    "delete_level": "none",
                    "field_masks": {},
                },
            )
            current["can_create"] = current["can_create"] or bool(row["can_create"])
            for action in ("read", "edit", "delete"):
                key = f"{action}_level"
                current[key] = widest(current[key], row[key])

        cur.execute(
            "SELECT m.object, m.field, m.access "
            "FROM core.role_field_masks m "
            "JOIN core.user_roles ur ON ur.role_id = m.role_id "
            "WHERE ur.user_id = %s",
            (user_id,),
        )
        mask_rows = cur.fetchall()

    # A mask is a ceiling, and the merge is permissive like everything else:
    # holding one role that does not restrict a field means the field is not
    # restricted. So a mask survives only if EVERY role the user holds masks it,
    # and the weakest restriction wins among those.
    with pool.transaction(org_id=org_id, user_id=user_id, readonly=True) as cur:
        cur.execute("SELECT count(*) AS n FROM core.user_roles WHERE user_id = %s", (user_id,))
        role_count = int((cur.fetchone() or {}).get("n") or 0)

    tally: dict[tuple[str, str], list[str]] = {}
    for row in mask_rows:
        tally.setdefault((row["object"], row["field"]), []).append(row["access"])
    for (obj, field_name), accesses in tally.items():
        if role_count and len(accesses) < role_count:
            continue  # some role leaves this field unrestricted -> unrestricted
        merged.setdefault(
            obj,
            {
                "can_create": False,
                "read_level": "none",
                "edit_level": "none",
                "delete_level": "none",
                "field_masks": {},
            },
        )
        merged[obj]["field_masks"][field_name] = "read" if "read" in accesses else "none"

    permissions = {
        obj: ObjectPermissions(
            object=obj,
            can_create=values["can_create"],
            read_level=values["read_level"],
            edit_level=values["edit_level"],
            delete_level=values["delete_level"],
            field_masks=values["field_masks"],
        )
        for obj, values in merged.items()
    }
    return Principal(
        user_id=user_id,
        org_id=org_id,
        is_admin=False,
        team_id=team_id,
        permissions=permissions,
    )


def visibility_predicate(
    principal: Principal,
    obj: str,
    action: str = "read",
    *,
    alias: str = "t",
) -> tuple[str, list[Any]]:
    """The SQL fragment restricting `obj` rows to what `principal` may see.

    Returns (sql, params) for direct use in a WHERE clause. The org boundary is
    NOT included -- RLS already enforces it, and duplicating it here would be a
    second implementation of the same rule (R1).

        'all'   -> TRUE
        'team'  -> owner is in my team, or the row is mine
        'own'   -> owner is me
        'none'  -> FALSE

    'none' returns a literal FALSE rather than raising, so a list endpoint
    returns an empty page instead of a 403. Callers that need a hard failure
    call principal.require() first -- which is what the repository does.
    """
    level = principal.for_object(obj).level_for(action)

    if level == "all":
        return "TRUE", []
    if level == "none":
        return "FALSE", []
    if level == "own":
        return f"{alias}.owner_id = %s", [principal.user_id]

    # 'team'. A user with no team degrades to 'own' -- there is no team whose
    # members they could see, and treating a null team as "matches everything
    # with a null team" would expose every unassigned record.
    if principal.team_id is None:
        return f"{alias}.owner_id = %s", [principal.user_id]
    return (
        f"({alias}.owner_id = %s OR {alias}.owner_id IN "
        f"(SELECT u.id FROM core.users u WHERE u.team_id = %s))",
        [principal.user_id, principal.team_id],
    )


def mask_record(principal: Principal, obj: str, record: dict[str, Any]) -> dict[str, Any]:
    """Strip fields this principal may not read. Applied on the way out, at the
    same choke point the visibility predicate is applied on the way in."""
    perms = principal.for_object(obj)
    if not perms.field_masks:
        return record
    return {k: v for k, v in record.items() if perms.readable_field(k)}


def reject_masked_writes(principal: Principal, obj: str, changes: dict[str, Any]) -> None:
    """Raise if `changes` touches a field this principal may not write.

    Silently dropping the field instead would report success for a write that
    did not happen, which is the same class of defect as a no-op gate.
    """
    perms = principal.for_object(obj)
    if not perms.field_masks:
        return
    blocked = sorted(k for k in changes if not perms.writable_field(k))
    if blocked:
        raise PermissionDenied("edit", obj, f"read-only field(s): {', '.join(blocked)}")

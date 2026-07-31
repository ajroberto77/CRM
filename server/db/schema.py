"""The authoritative schema, and the migration that applies it.

## The pattern

Every table is one dict: column name -> column DDL. The FIRST entry is the
primary key. Migration creates the table with just its primary key, then adds
every other column with `ADD COLUMN IF NOT EXISTS`. Fresh installs and existing
databases therefore go through the exact same path, and schema growth is adding
a key to a dict. Ported from the sibling projects, which proved it across
several years of incremental change.

This file is the authoritative source for table and column names. Design docs
describe intent; this describes reality. Never guess a column -- read it here,
or read the live database with psql.

## Tenancy

Every table carries `org_id`, and every table has RLS enabled AND forced. Two
details that are the difference between a policy and a suggestion:

  * Without FORCE, the table owner bypasses every policy. If the app role also
    owns the tables, ENABLE alone does nothing at all.
  * `org_id` leads every composite index. A policy predicate on a non-leading
    index column is orders of magnitude slower -- this is the single
    most-reported RLS performance failure, not a micro-optimization.

Record-level visibility (own/team) is deliberately NOT here. It is predicate
injection in server/core/repository.py, where it can be read, indexed and
debugged. RLS carries the org boundary only.
"""
from __future__ import annotations

from typing import Iterable, Optional

from server.db import pool

# Logical schemas. One database; these keep the namespaces apart.
#
# `auth` is deliberately different from the rest: it holds IDENTITY AND ROUTING
# data and is NOT under org RLS, because it is what establishes tenant context
# in the first place. A credential has to be resolvable before you know which
# tenant it belongs to; making that lookup tenant-scoped is circular, and the
# usual workarounds -- a BYPASSRLS role, or a policy that opens up whenever no
# context is set -- are both far worse than a small, explicit, minimal-content
# identity layer.
#
# The rule that keeps this honest: `auth` tables carry ONLY what is needed to
# route a login to a tenant. No PII beyond the email, no password hash, no
# profile. Everything else lives in `core` under RLS.
SCHEMAS = ("auth", "core", "sync", "jobs", "ai")

# Tables outside the org-RLS boundary, and why. Anything added here is a
# deliberate hole in tenant isolation and must be justified in this comment.
UNSCOPED_TABLES = frozenset(
    {
        # email -> which tenant. The minimum information required to route a
        # login. Read once, before context exists; never listed.
        "auth.identities",
        # token hash -> which tenant/user. Same reasoning: the bearer token is
        # what identifies the org, so its lookup cannot be org-scoped.
        "auth.sessions",
    }
)

# The tenant predicate, shared by every policy. NULLIF guards the unset case:
# current_setting(..., true) returns NULL when the GUC was never set, but an
# empty string when it was set to ''. Casting '' to uuid raises; NULL simply
# makes the comparison NULL, so an uncontexted connection sees zero rows --
# a silent empty result, which is the correct fail-closed default.
_ORG_PREDICATE = "org_id = NULLIF(current_setting('app.org_id', true), '')::uuid"
_ORG_SELF_PREDICATE = "id = NULLIF(current_setting('app.org_id', true), '')::uuid"

# core.orgs looks like it has a bootstrap problem -- creating an org cannot
# satisfy a policy requiring you to already be in that org -- but it does not,
# and the fix is NOT to widen the policy. server/core/users.py generates the
# org's UUID client-side and opens the transaction with context already set to
# it, so the INSERT and its RETURNING both pass an unmodified symmetric policy.
#
# Worth knowing, because it cost an hour: `INSERT ... RETURNING` is subject to
# the SELECT policy as well as WITH CHECK. An insert that satisfies WITH CHECK
# still fails if USING rejects reading the new row back, and the error message
# blames the row-level security policy without mentioning RETURNING.

# Visibility levels for the EspoCRM-shaped permission model. Closed vocabulary
# on purpose -- free text would drift and silently stop matching.
VISIBILITY_LEVELS = ("all", "team", "own", "none")

_UUID_PK = "uuid PRIMARY KEY DEFAULT gen_random_uuid()"
_ORG_FK = "uuid NOT NULL REFERENCES core.orgs(id) ON DELETE CASCADE"
_CREATED = "timestamptz NOT NULL DEFAULT now()"


def _level(column: str, default: str) -> str:
    return (
        f"text NOT NULL DEFAULT '{default}' "
        f"CHECK ({column} IN ('all','team','own','none'))"
    )


# ── Tables ───────────────────────────────────────────────────────────────────
# Insertion order is creation order; foreign keys must point backwards.

TABLES: dict[str, dict[str, str]] = {
    # ── Identity layer (no RLS -- see UNSCOPED_TABLES) ────────────────────
    # Deliberately minimal: a normalized email, and which tenant/user it routes
    # to. The password hash is NOT here; it stays in core.users under RLS and is
    # read only after context has been established from this row.
    "auth.identities": {
        "id": _UUID_PK,
        "email": "text NOT NULL DEFAULT ''",
        "org_id": "uuid NOT NULL",
        "user_id": "uuid NOT NULL",
        "created_at": _CREATED,
    },

    # Bearer sessions. Outside RLS for the same reason: the token is what
    # identifies the org, so its lookup cannot be org-scoped. Only the hash is
    # stored -- a database disclosure must not hand over live sessions.
    "auth.sessions": {
        "id": _UUID_PK,
        "token_hash": "text NOT NULL DEFAULT ''",
        "org_id": "uuid NOT NULL",
        "user_id": "uuid NOT NULL",
        "created_at": _CREATED,
        "expires_at": "timestamptz NOT NULL DEFAULT now()",
        "last_seen_at": _CREATED,
        "revoked_at": "timestamptz",
        "user_agent": "text NOT NULL DEFAULT ''",
        "ip": "text NOT NULL DEFAULT ''",
    },

    # ── Tenant data (all under RLS) ──────────────────────────────────────
    # The tenant. A single org is seeded at first run; the column and its
    # policies exist from day one because retrofitting them is a migration
    # across every table, while carrying them costs nothing.
    "core.orgs": {
        "id": _UUID_PK,
        "name": "text NOT NULL DEFAULT ''",
        "slug": "text NOT NULL DEFAULT ''",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    # Teams exist because record visibility has a 'team' level. A user without
    # a team behaves as 'own' at that level.
    "core.teams": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "name": "text NOT NULL DEFAULT ''",
        "created_at": _CREATED,
    },

    "core.users": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        # email is the NORMALIZED form (server/core/identity.py) and is what
        # uniqueness and lookup use. email_raw preserves what was typed, for
        # display. Never match on email_raw; never show the user `email`.
        "email": "text NOT NULL DEFAULT ''",
        "email_raw": "text NOT NULL DEFAULT ''",
        "name": "text NOT NULL DEFAULT ''",
        # Null password_hash = no password set yet (invited but not activated).
        # It must never authenticate -- see server/api/auth.py.
        "password_hash": "text",
        "status": (
            "text NOT NULL DEFAULT 'active' "
            "CHECK (status IN ('active','invited','disabled'))"
        ),
        "team_id": "uuid REFERENCES core.teams(id) ON DELETE SET NULL",
        # The hard admin/non-admin split. Admin reaches settings, provider
        # credentials and the secrets surface; roles govern everything else.
        "is_admin": "boolean NOT NULL DEFAULT false",
        "created_at": _CREATED,
        "updated_at": _CREATED,
        "last_login_at": "timestamptz",
    },

    "core.roles": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "name": "text NOT NULL DEFAULT ''",
        "description": "text NOT NULL DEFAULT ''",
        "created_at": _CREATED,
    },

    # One row per (role, object). Multiple roles merge PERMISSIVELY: if any
    # role grants an action, the user has it, and the widest visibility level
    # wins. Absence of a row means no access -- default deny.
    "core.role_scopes": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "role_id": "uuid NOT NULL REFERENCES core.roles(id) ON DELETE CASCADE",
        # The registered entity name (server/core/registry.py), e.g. 'person'.
        "object": "text NOT NULL DEFAULT ''",
        "can_create": "boolean NOT NULL DEFAULT false",
        "read_level": _level("read_level", "none"),
        "edit_level": _level("edit_level", "none"),
        "delete_level": _level("delete_level", "none"),
        "created_at": _CREATED,
    },

    # Field-level masking, for the handful of fields that need it. `access` is
    # the CEILING this role has on the field: 'none' hides it entirely,
    # 'read' makes it read-only. A field with no row is unrestricted.
    "core.role_field_masks": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "role_id": "uuid NOT NULL REFERENCES core.roles(id) ON DELETE CASCADE",
        "object": "text NOT NULL DEFAULT ''",
        "field": "text NOT NULL DEFAULT ''",
        "access": (
            "text NOT NULL DEFAULT 'read' CHECK (access IN ('none','read'))"
        ),
        "created_at": _CREATED,
    },

    "core.user_roles": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "user_id": "uuid NOT NULL REFERENCES core.users(id) ON DELETE CASCADE",
        "role_id": "uuid NOT NULL REFERENCES core.roles(id) ON DELETE CASCADE",
        "created_at": _CREATED,
    },

}

# ── Indexes ──────────────────────────────────────────────────────────────────
# org_id leads every composite index. See the module docstring.

INDEXES: tuple[str, ...] = (
    # Identity lookups happen before any org context exists, so these are
    # global unique indexes rather than org-leading. That is the whole reason
    # these two tables sit outside the RLS boundary.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_identities_email ON auth.identities (email)",
    "CREATE INDEX IF NOT EXISTS ix_identities_user ON auth.identities (user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_token ON auth.sessions (token_hash)",
    "CREATE INDEX IF NOT EXISTS ix_sessions_org_user "
    "ON auth.sessions (org_id, user_id, expires_at DESC)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_orgs_slug ON core.orgs (slug) WHERE slug <> ''",

    "CREATE INDEX IF NOT EXISTS ix_teams_org ON core.teams (org_id, name)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_org_email ON core.users (org_id, email)",
    "CREATE INDEX IF NOT EXISTS ix_users_org_status ON core.users (org_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_users_org_team ON core.users (org_id, team_id)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_roles_org_name ON core.roles (org_id, name)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_role_scopes_role_object "
    "ON core.role_scopes (org_id, role_id, object)",
    "CREATE INDEX IF NOT EXISTS ix_role_scopes_org_object "
    "ON core.role_scopes (org_id, object)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_role_field_masks "
    "ON core.role_field_masks (org_id, role_id, object, field)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_roles ON core.user_roles (org_id, user_id, role_id)",
    "CREATE INDEX IF NOT EXISTS ix_user_roles_org_role ON core.user_roles (org_id, role_id)",

)

# ── Row-level security ───────────────────────────────────────────────────────
# table -> the predicate isolating one tenant. Every table that is not in
# UNSCOPED_TABLES appears here; a scoped table missing from this map is a bug,
# and verify_rls() reports it.

RLS_PREDICATES: dict[str, str] = {
    "core.orgs": _ORG_SELF_PREDICATE,
    **{
        name: _ORG_PREDICATE
        for name in TABLES
        if name != "core.orgs" and name not in UNSCOPED_TABLES
    },
}


# ── Migration ────────────────────────────────────────────────────────────────

def _pk_clause(table: str, columns: dict[str, str]) -> str:
    pk_name, pk_decl = next(iter(columns.items()))
    return f"CREATE TABLE IF NOT EXISTS {table} ({pk_name} {pk_decl})"


def _ensure_schemas(cur) -> None:
    for name in SCHEMAS:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {name}")


def _ensure_table(cur, table: str, columns: dict[str, str]) -> None:
    cur.execute(_pk_clause(table, columns))
    for name, decl in list(columns.items())[1:]:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {decl}")


def _ensure_rls(cur, table: str, predicate: str) -> None:
    cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    # FORCE is the half that is easy to omit and fatal to omit: without it the
    # table owner -- frequently the app role itself -- bypasses every policy.
    cur.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    short = table.split(".")[-1]
    policy = f"{short}_tenant_isolation"
    # DROP + CREATE rather than CREATE OR REPLACE, which Postgres does not
    # offer for policies. Idempotent, and it lets a predicate change ship as an
    # ordinary migration.
    cur.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    cur.execute(
        f"CREATE POLICY {policy} ON {table} USING ({predicate}) WITH CHECK ({predicate})"
    )
    # Converge databases built by an earlier revision of this file, rather than
    # leaving both shapes in place.
    cur.execute(f"DROP POLICY IF EXISTS {short}_bootstrap_insert ON {table}")


def migrate(*, tables: Optional[Iterable[str]] = None) -> None:
    """Bring the database up to the schema declared above. Idempotent: safe to
    run on every boot, and the only supported way to change the schema."""
    wanted = list(tables) if tables is not None else list(TABLES)
    with pool.system_transaction() as cur:
        _ensure_schemas(cur)
        for table in wanted:
            _ensure_table(cur, table, TABLES[table])
        for statement in INDEXES:
            cur.execute(statement)
        for table in wanted:
            if table in UNSCOPED_TABLES:
                continue
            _ensure_rls(cur, table, RLS_PREDICATES[table])


# ── Verification ─────────────────────────────────────────────────────────────

def verify_rls() -> list[dict[str, object]]:
    """Report every declared table whose RLS is not fully armed. An empty list
    is the only acceptable result in a deployed system; anything else is a
    tenant-isolation hole. Exposed so it can be asserted in tests and surfaced
    at startup rather than trusted.
    """
    problems: list[dict[str, object]] = []
    with pool.system_transaction() as cur:
        cur.execute(
            "SELECT n.nspname || '.' || c.relname AS table_name, "
            "       c.relrowsecurity AS enabled, c.relforcerowsecurity AS forced, "
            "       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = ANY(%s) AND c.relkind = 'r'",
            (list(SCHEMAS),),
        )
        actual = {row["table_name"]: dict(row) for row in cur.fetchall()}

    for table in TABLES:
        state = actual.get(table)
        if state is None:
            problems.append({"table": table, "problem": "missing"})
            continue
        if table in UNSCOPED_TABLES:
            # Deliberately outside the boundary. Flag the reverse mistake:
            # silently gaining a policy would break login in a way that looks
            # like an authentication bug rather than a schema change.
            if state["enabled"] or state["policies"]:
                problems.append({"table": table, "problem": "unexpectedly_scoped"})
            continue
        if not state["enabled"]:
            problems.append({"table": table, "problem": "rls_not_enabled"})
        if not state["forced"]:
            # The owner-bypass hole. Worth its own code because it is invisible
            # in every functional test -- queries succeed, they just see too much.
            problems.append({"table": table, "problem": "rls_not_forced"})
        if not state["policies"]:
            problems.append({"table": table, "problem": "no_policy"})
    return problems


def verify_index_leading_org() -> list[dict[str, object]]:
    """Report composite indexes on tenant tables whose leading column is not
    `org_id`. Not every such index is wrong -- core.sessions is looked up by
    token before any org context exists -- so this reports rather than raises,
    and known exceptions are listed here explicitly."""
    allowed = {"uq_sessions_token", "uq_orgs_slug"}
    problems: list[dict[str, object]] = []
    with pool.system_transaction() as cur:
        cur.execute(
            "SELECT n.nspname || '.' || t.relname AS table_name, i.relname AS index_name, "
            "       a.attname AS first_column "
            "FROM pg_index x "
            "JOIN pg_class i ON i.oid = x.indexrelid "
            "JOIN pg_class t ON t.oid = x.indrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = x.indkey[0] "
            "WHERE n.nspname = ANY(%s) AND x.indnatts > 1",
            (list(SCHEMAS),),
        )
        rows = [dict(r) for r in cur.fetchall()]

    for row in rows:
        if row["index_name"] in allowed:
            continue
        if row["table_name"] == "core.orgs":
            continue
        if row["first_column"] != "org_id":
            problems.append(
                {
                    "table": row["table_name"],
                    "index": row["index_name"],
                    "first_column": row["first_column"],
                    "problem": "org_id_not_leading",
                }
            )
    return problems

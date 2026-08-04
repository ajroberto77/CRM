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

import psycopg2.errors

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
        # M7's work queue (server/jobs/queue.py). A single generic worker
        # pool claims a batch across EVERY org in one query -- the same
        # tension `all_org_ids()` (server/jobs/__init__.py) already found
        # and documented: an org-scoped table is invisible to a query with
        # no tenant GUC set, so a claim query could never see more than
        # zero rows if this carried org_id under the normal RLS predicate.
        # Mirrors CATO's own `cato_coordinator` database, deliberately
        # separate from each pipeline's real domain output: coordination
        # data (which job is pending/claimed/done) lives outside the
        # tenant boundary; every payload that touches real tenant data
        # carries its own `org_id` key, and a handler opens its own
        # `pool.transaction(org_id=...)` to do that work -- queue.py
        # itself never does.
        "jobs.work_queue",
        "jobs.workers",
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

# M7's pgvector search (`ai.embeddings`). pgvector's HNSW index requires
# every indexed vector to share one fixed dimension, so this is a single
# platform-wide choice, not a per-org setting -- changing it means
# dropping and rebuilding the column and every stored embedding.
# 768 matches Ollama's default embedding model (nomic-embed-text);
# `server/llm/embeddings.py` explicitly requests this many dimensions
# from OpenAI too (its v3 embedding models support a `dimensions`
# parameter for exactly this kind of truncation), so both providers
# write into the same column shape.
EMBEDDING_DIM = 768


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

    # ── Records ──────────────────────────────────────────────────────────
    # Every record entity shares a spine: org_id, owner_id (what the own/team
    # visibility predicate reads), custom jsonb, updated_at (optimistic
    # concurrency), and timestamps. registry.verify() refuses to start if a
    # registered entity is missing any of them -- `visibility_predicate`
    # hardcodes `owner_id`, so an entity without it produces invalid SQL only
    # when a non-admin queries it, which no test using the admin fixture sees.

    # Declared before core.deals: _ensure_table() creates in insertion order and
    # the FK points backwards.
    "core.pipelines": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "name": "text NOT NULL DEFAULT ''",
        "stages": "jsonb NOT NULL DEFAULT '[]'::jsonb",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    "core.organizations": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "name": "text NOT NULL DEFAULT ''",
        "name_normalized": "text NOT NULL DEFAULT ''",
        "domain": "text NOT NULL DEFAULT ''",
        "description": "text NOT NULL DEFAULT ''",
        # Marks entities the firm itself controls (its own funds and vehicles),
        # so the vertical can tell "our fund" from "another GP's fund" without
        # core knowing what a fund is.
        "is_internal": "boolean NOT NULL DEFAULT false",
        "source": (
            "text NOT NULL DEFAULT 'human' "
            "CHECK (source IN ('human','derived','import','sync','ai'))"
        ),
        "is_derived": "boolean NOT NULL DEFAULT false",
        # ISO-3166-1 alpha-2 (server/core/identity.py's normalize_country),
        # formation jurisdiction -- distinct from a person's tax residence or
        # citizenship below, and from an investment_account's own domicile
        # (Phase B), which can legitimately differ from its parent entity's.
        "domicile_country": "text NOT NULL DEFAULT ''",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    "core.persons": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "full_name": "text NOT NULL DEFAULT ''",
        "name_normalized": "text NOT NULL DEFAULT ''",
        "title": "text NOT NULL DEFAULT ''",
        # Display denormalization only. contact_channels (M2) is the matching
        # surface; never match on this column.
        "primary_email": "text NOT NULL DEFAULT ''",
        "description": "text NOT NULL DEFAULT ''",
        "source": (
            "text NOT NULL DEFAULT 'human' "
            "CHECK (source IN ('human','derived','import','sync','ai'))"
        ),
        "is_derived": "boolean NOT NULL DEFAULT false",
        # M5's trust primitive (server/core/trust.py): a specific person
        # whose scheduling proposals auto-accept regardless of the org-wide
        # trusted_senders patterns below. Default false -- trust is opt-in,
        # never inferred (safety rule 9: destructive/consequential paths
        # default off).
        "auto_accept": "boolean NOT NULL DEFAULT false",
        # ISO-3166-1 alpha-2, two genuinely different facts that happen to
        # share a representation: where this person pays tax (drives which
        # W-8/W-9 variant Phase D requires) vs. their nationality. A US
        # citizen resident abroad, or a non-US citizen resident in the US,
        # needs both tracked separately -- neither implies the other.
        "tax_residence_country": "text NOT NULL DEFAULT ''",
        "citizenship_country": "text NOT NULL DEFAULT ''",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    "core.deals": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "name": "text NOT NULL DEFAULT ''",
        "pipeline_id": "uuid REFERENCES core.pipelines(id) ON DELETE SET NULL",
        "stage": "text NOT NULL DEFAULT ''",
        "amount": "numeric(18,2)",
        "currency": "text NOT NULL DEFAULT 'USD'",
        "expected_close_on": "date",
        "status": (
            "text NOT NULL DEFAULT 'open' CHECK (status IN ('open','won','lost'))"
        ),
        # Deal rotting reads this. A real column, not JSONB: high-churn derived
        # data, and a JSONB update rewrites the whole value under a row lock.
        "last_activity_at": "timestamptz",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    "core.tasks": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "title": "text NOT NULL DEFAULT ''",
        "body": "text NOT NULL DEFAULT ''",
        "due_on": "date",
        "status": (
            "text NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled'))"
        ),
        "subject_type": "text NOT NULL DEFAULT ''",
        "subject_id": "uuid",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    "core.notes": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "body": "text NOT NULL DEFAULT ''",
        "subject_type": "text NOT NULL DEFAULT ''",
        "subject_id": "uuid",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    # Dated, role-bearing relationships. This one table is why there is no
    # investors table, no portfolio_companies table, no employments table and no
    # co_investors table -- see docs/VERTICAL-ASSET-MANAGEMENT.md.
    #
    # ONE row per relationship, queried from both ends through
    # core.association_edges. Storing both directions doubles every write and
    # the copies drift the first time one is edited.
    #
    # No owner_id, deliberately: an association is not a permission subject.
    # Access is derived -- readable iff both endpoints are readable -- so it is
    # never passed to visibility_predicate(), which would emit a predicate on a
    # column that does not exist here.
    "core.associations": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "from_type": "text NOT NULL DEFAULT ''",
        "from_id": "uuid NOT NULL",
        "to_type": "text NOT NULL DEFAULT ''",
        "to_id": "uuid NOT NULL",
        "role": "text NOT NULL DEFAULT ''",
        "attributes": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "valid_from": "date",
        "valid_to": "date",
        "created_at": _CREATED,
        "created_by": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "updated_at": _CREATED,
    },

    # User-defined fields. `key` is constrained at the application layer to
    # ^[a-z][a-z0-9_]{0,48}$ -- these keys reach SQL as index names and as bound
    # JSONB paths, and a key like `x' OR '1'='1` would be a stored injection
    # that never looks like a request parameter.
    "core.custom_fields": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        # Present because custom_fields is a registered entity like any other,
        # and visibility_predicate() emits a predicate on owner_id for every
        # non-admin. registry.verify() refuses to start without it.
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "entity": "text NOT NULL DEFAULT ''",
        "key": "text NOT NULL DEFAULT ''",
        "label": "text NOT NULL DEFAULT ''",
        "kind": (
            "text NOT NULL DEFAULT 'text' "
            "CHECK (kind IN ('text','number','date','datetime','boolean',"
            "'select','multiselect','currency','url','email','phone'))"
        ),
        "options": "jsonb NOT NULL DEFAULT '[]'::jsonb",
        "indexed": "boolean NOT NULL DEFAULT false",
        "index_name": "text NOT NULL DEFAULT ''",
        "index_state": (
            "text NOT NULL DEFAULT 'none' "
            "CHECK (index_state IN ('none','pending','building','active','invalid','failed','retiring'))"
        ),
        "index_error": "text NOT NULL DEFAULT ''",
        "archived_at": "timestamptz",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    "core.saved_views": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "entity": "text NOT NULL DEFAULT ''",
        "name": "text NOT NULL DEFAULT ''",
        "kind": (
            "text NOT NULL DEFAULT 'table' CHECK (kind IN ('table','board','calendar'))"
        ),
        # Stored filter JSON, recompiled and re-permission-checked on every
        # execution by the executing user. Never cached as SQL: a view saved by
        # an admin must not read a masked field for a member.
        "filters": "jsonb NOT NULL DEFAULT '[]'::jsonb",
        "sort": "jsonb NOT NULL DEFAULT '[]'::jsonb",
        "columns": "jsonb NOT NULL DEFAULT '[]'::jsonb",
        "group_by": "text NOT NULL DEFAULT ''",
        "is_shared": "boolean NOT NULL DEFAULT true",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    # Generic, subject-polymorphic document storage -- core, not vertical. A
    # subscription agreement is a document whose `kind` a module chose to set;
    # this table has no idea what a commitment is (docs/INVESTOR-PORTAL.md).
    # `provider`/`provider_envelope_id` are free text rather than an FK or
    # enum, so a new esign provider (R3's sixth axis) is never a schema change
    # here -- only a new `<provider>_esign.py` adapter.
    "core.documents": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "subject_type": "text NOT NULL DEFAULT ''",
        "subject_id": "uuid",
        "kind": "text NOT NULL DEFAULT ''",
        "filename": "text NOT NULL DEFAULT ''",
        "storage_key": "text NOT NULL DEFAULT ''",
        "mime": "text NOT NULL DEFAULT ''",
        "bytes": "bigint",
        "provider": "text NOT NULL DEFAULT ''",
        "provider_envelope_id": "text NOT NULL DEFAULT ''",
        "status": (
            "text NOT NULL DEFAULT 'draft' "
            "CHECK (status IN ('draft','sent_for_signature','partially_signed',"
            "'executed','void','expired'))"
        ),
        "valid_from": "date",
        "valid_until": "date",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    # Per-signer tracking for a document sent for e-signature. Not a registered
    # entity (no owner_id, no generic CRUD) -- same treatment as
    # core.associations: internal detail of a workflow, managed by its own
    # module (server/core/documents.py) rather than through the generic
    # repository. `role` is free text ('investor', 'gp', 'witness', ...).
    "core.document_signers": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "document_id": "uuid NOT NULL REFERENCES core.documents(id) ON DELETE CASCADE",
        "subject_type": "text NOT NULL DEFAULT ''",
        "subject_id": "uuid",
        "role": "text NOT NULL DEFAULT ''",
        "order_index": "integer NOT NULL DEFAULT 0",
        "status": (
            "text NOT NULL DEFAULT 'pending' "
            "CHECK (status IN ('pending','sent','viewed','signed','declined'))"
        ),
        "signed_at": "timestamptz",
        "provider_signer_id": "text NOT NULL DEFAULT ''",
        "created_at": _CREATED,
    },

    # M2's primitive: the CRM is a derived view over this log, not a system of
    # record typed into directly (docs/DESIGN.md). `body` is owner-scoped via
    # FieldSpec.read_level="own" in the registry -- every other column is
    # org-wide (safety rule 10). `body`'s owner check compares against this
    # table's own `owner_id` (the spine column every entity carries) rather
    # than a second "whose mailbox observed this" column: they are the same
    # fact, and a table carrying both risks them silently disagreeing.
    # `owner_id` is NOT NULL here, unlike the spine's usual nullable default
    # (see registry.py's `interaction` registration) -- an interaction with
    # no owner has no one the body could ever be visible to.
    # `account_id` has no FK yet: there is no connected-mailbox table until
    # M4, so it stays a loose reference, forward-compatible with one once it
    # exists.
    "core.interactions": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "owner_id": "uuid NOT NULL REFERENCES core.users(id) ON DELETE CASCADE",
        "account_id": "uuid",
        "occurred_at": "timestamptz NOT NULL DEFAULT now()",
        "kind": (
            "text NOT NULL DEFAULT 'email' "
            "CHECK (kind IN ('email','call','meeting','sms','chat','other'))"
        ),
        "direction": (
            "text NOT NULL DEFAULT 'inbound' "
            "CHECK (direction IN ('inbound','outbound','internal'))"
        ),
        "thread_id": "text NOT NULL DEFAULT ''",
        "from_channel": "text NOT NULL DEFAULT ''",
        "to_channels": "jsonb NOT NULL DEFAULT '[]'::jsonb",
        "subject_hash": "text NOT NULL DEFAULT ''",
        "body": "text NOT NULL DEFAULT ''",
        "external_id": "text NOT NULL DEFAULT ''",
        "source": (
            "text NOT NULL DEFAULT 'human' CHECK (source IN ('human','sync','ai'))"
        ),
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    # Omnichannel identity resolution -- an inbound Signal message, a Telegram
    # chat and an email thread all resolve to one person through this table.
    # `value_normalized` is written only via server/core/identity.py's
    # normalizers (R5); never trust a client-supplied normalized value.
    "core.contact_channels": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "person_id": "uuid NOT NULL REFERENCES core.persons(id) ON DELETE CASCADE",
        "kind": (
            "text NOT NULL DEFAULT 'email' "
            "CHECK (kind IN ('email','phone','signal','telegram','handle'))"
        ),
        "value_normalized": "text NOT NULL DEFAULT ''",
        "value_raw": "text NOT NULL DEFAULT ''",
        "is_primary": "boolean NOT NULL DEFAULT false",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    # Who was actually on an interaction -- resolved automatically by
    # server/core/derivation.py from `interactions.from_channel`/`to_channels`
    # (the same contact_channel lookup that materializes a person for a new
    # channel value), recorded here instead of thrown away so "every
    # interaction this person was part of" is a query, not a re-derivation.
    # Not a registered entity (server/core/interactions.py owns its SQL,
    # same treatment as `core.document_signers`/`core.associations`): an
    # internal detail nobody creates or edits directly. No owner_id --
    # who-talked-to-whom is org-wide contact-graph metadata (safety rule 10),
    # exactly like `core.associations` has none for the same reason; a
    # participant row names WHICH interaction and WHICH person, never a
    # body, so there is nothing here that needs owner-scoping the way
    # `interactions.body` does.
    "core.interaction_participants": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "interaction_id": "uuid NOT NULL REFERENCES core.interactions(id) ON DELETE CASCADE",
        "person_id": "uuid NOT NULL REFERENCES core.persons(id) ON DELETE CASCADE",
        "role": "text NOT NULL DEFAULT 'to' CHECK (role IN ('from','to'))",
        "created_at": _CREATED,
    },

    # Period-shaped data, deliberately not squeezed into `interactions` (event-
    # shaped) or a record's `custom` (would need an expression index per
    # metric). "Acme's Q3 EBITDA" -- there is a new one every quarter, so it is
    # not a property of the company either.
    "core.metric_facts": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "subject_type": "text NOT NULL DEFAULT ''",
        "subject_id": "uuid",
        "period_start": "date",
        "period_end": "date",
        "metric_key": "text NOT NULL DEFAULT ''",
        "value_numeric": "numeric(24,4)",
        "value_text": "text NOT NULL DEFAULT ''",
        "currency": "text NOT NULL DEFAULT ''",
        "source": (
            "text NOT NULL DEFAULT 'human' CHECK (source IN ('human','sync','ai'))"
        ),
        "confidence": "numeric(4,3)",
        "document_id": "uuid REFERENCES core.documents(id) ON DELETE SET NULL",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    # Per-org product settings (server/core/settings.py), one row per
    # (org_id, section). Not a registered entity (R4) -- a settings section is
    # a singleton an admin edits in place, not a list of records a
    # table/detail UI would browse, and the read-merge-write discipline
    # (safety rule 2) doesn't fit the generic repository's per-column merge
    # semantics. `id` is a synthetic PK (schema.py's DDL generator only
    # supports a single-column PK); the real identity constraint is the
    # unique index on (org_id, section) below.
    "core.settings": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "section": "text NOT NULL DEFAULT ''",
        "values": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "updated_at": _CREATED,
        "updated_by": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
    },

    # The transactional outbox. Written INSIDE the repository's transaction, so
    # a record write and the event describing it commit or roll back together;
    # dispatched after commit. This is what makes an M5 subscriber that enqueues
    # a job or sends a message correct rather than best-effort.
    "core.events": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "seq": "bigserial",
        "occurred_at": _CREATED,
        "entity": "text NOT NULL DEFAULT ''",
        "record_id": "uuid",
        "action": "text NOT NULL DEFAULT ''",
        "actor_user_id": "uuid",
        "actor_kind": "text NOT NULL DEFAULT 'user'",
        "diff": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        # M7's event redelivery (server/jobs -- draining this outbox for
        # at-least-once delivery) needs the full record, not just the
        # diff: a CREATE's diff is always empty, and an UPDATE's diff
        # only carries the CHANGED fields, not the unchanged ones a
        # subscriber like derivation.py's `event.after.get("kind")` also
        # reads. Without these, redelivery would hand every subscriber an
        # empty/partial record and silently do nothing. Populated from
        # the same before/after dicts record_event()'s caller already
        # holds in memory -- no second read.
        "before_snapshot": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "after_snapshot": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "caused_by_event_id": "uuid",
        "delivery_state": (
            "text NOT NULL DEFAULT 'pending' "
            "CHECK (delivery_state IN ('pending','delivered','failed'))"
        ),
        "attempts": "integer NOT NULL DEFAULT 0",
        "last_error": "text NOT NULL DEFAULT ''",
    },

    "core.user_roles": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "user_id": "uuid NOT NULL REFERENCES core.users(id) ON DELETE CASCADE",
        "role_id": "uuid NOT NULL REFERENCES core.roles(id) ON DELETE CASCADE",
        "created_at": _CREATED,
    },

    # M4: a user's own linked Microsoft/Google account -- unlike Cal's
    # family/assistant split, every CRM user links their own mailbox/
    # calendar/contacts, so the natural owner is `user_id`, not an org-wide
    # singleton. One row per (org_id, user_id, provider).
    "core.connected_accounts": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "user_id": "uuid NOT NULL REFERENCES core.users(id) ON DELETE CASCADE",
        "provider": (
            "text NOT NULL DEFAULT '' CHECK (provider IN ('microsoft','google'))"
        ),
        "email_address": "text NOT NULL DEFAULT ''",
        "status": (
            "text NOT NULL DEFAULT 'pending' "
            "CHECK (status IN ('pending','active','error','disabled'))"
        ),
        "status_detail": "text NOT NULL DEFAULT ''",
        "timezone": "text NOT NULL DEFAULT 'UTC'",
        "default_calendar_id": "text NOT NULL DEFAULT ''",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    # Split from connected_accounts (Cal's own reasoning, ported): rotation
    # tracking, and room for a future non-OAuth credential type without
    # reshaping the account row itself. `payload` holds `{"refresh_token",
    # "scopes"}` -- never the access token, which is minted per call and
    # never persisted.
    "core.account_credentials": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "account_id": (
            "uuid NOT NULL REFERENCES core.connected_accounts(id) ON DELETE CASCADE"
        ),
        "credential_type": (
            "text NOT NULL DEFAULT 'oauth' CHECK (credential_type IN ('oauth'))"
        ),
        "payload": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "obtained_at": _CREATED,
        "rotated_at": "timestamptz",
    },

    # A single-use PKCE code_verifier, held between the authorize redirect
    # and the callback. Swept past a 900s max age on every new link attempt
    # (server/core/account_link.py) -- this table is never large.
    "core.oauth_pending": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "account_id": (
            "uuid NOT NULL REFERENCES core.connected_accounts(id) ON DELETE CASCADE"
        ),
        "provider": (
            "text NOT NULL DEFAULT '' CHECK (provider IN ('microsoft','google'))"
        ),
        "state": "text NOT NULL DEFAULT ''",
        "code_verifier": "text NOT NULL DEFAULT ''",
        "created_at": _CREATED,
    },

    # One row per (account, resource) rather than a column per resource on
    # connected_accounts -- a future synced resource (mail, in a later
    # milestone) needs no migration here, just a new `resource` value.
    "core.sync_cursors": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "account_id": (
            "uuid NOT NULL REFERENCES core.connected_accounts(id) ON DELETE CASCADE"
        ),
        "resource": (
            "text NOT NULL DEFAULT '' CHECK (resource IN ('contacts','calendar'))"
        ),
        "cursor_token": "text NOT NULL DEFAULT ''",
        "updated_at": _CREATED,
    },

    # Maps an external contact/event id to the CRM record it produced, so a
    # re-sync updates the same row instead of duplicating it.
    "core.sync_links": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "account_id": (
            "uuid NOT NULL REFERENCES core.connected_accounts(id) ON DELETE CASCADE"
        ),
        "provider_object_id": "text NOT NULL DEFAULT ''",
        "entity_type": (
            "text NOT NULL DEFAULT '' CHECK (entity_type IN ('person','organization'))"
        ),
        "entity_id": "uuid NOT NULL",
        "created_at": _CREATED,
    },

    # M5's trust primitive (server/core/trust.py), ported from Cal's
    # trusted_senders.py. `pattern` is either a full email address or an
    # "@domain" -- normalize-at-write (server/core/trust.py's add_sender()),
    # never re-normalized at read time.
    "core.trusted_senders": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "pattern": "text NOT NULL DEFAULT ''",
        "label": "text NOT NULL DEFAULT ''",
        "note": "text NOT NULL DEFAULT ''",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    # server/core/proposals.py's "one approval queue." Polymorphic over
    # subject_type/subject_id -- calendar_event (M5) today, extensible.
    # `category` is deliberately open text, not CHECK-constrained: the
    # closed vocabulary belongs to whichever producer creates a proposal
    # (server/extraction/scheduling.py's EVENT_CATEGORIES today), not to
    # this domain-neutral queue.
    "core.proposed_changes": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "subject_type": "text NOT NULL DEFAULT ''",
        "subject_id": "uuid",
        "kind": "text NOT NULL DEFAULT ''",
        "payload": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "confidence": "numeric(4,3)",
        "category": "text NOT NULL DEFAULT ''",
        "status": (
            "text NOT NULL DEFAULT 'pending' "
            "CHECK (status IN ('pending','approved','declined','auto_approved'))"
        ),
        "decided_by": "text NOT NULL DEFAULT ''",
        "decided_at": "timestamptz",
        "notification_message_id": "text NOT NULL DEFAULT ''",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    # M6's messaging axis (server/channels/dispatch.py): resolves an
    # inbound Signal/Telegram sender to a CRM user. Distinct from
    # contact_channels, which maps a channel to a third party's `person`
    # -- this maps a channel to a LOGIN. Deliberately NOT a registered
    # entity, same reasoning as core.connected_accounts: this is
    # identity/auth-adjacent, not generic CRM data a broad edit scope
    # should be able to reassign. server/core/user_channels.py is the one
    # place that writes it, and server/api/channels.py never accepts a
    # target user_id from the request body -- every link is the calling
    # principal linking themselves.
    "core.user_channels": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "user_id": "uuid NOT NULL REFERENCES core.users(id) ON DELETE CASCADE",
        "kind": (
            "text NOT NULL DEFAULT '' "
            "CHECK (kind IN ('email','phone','signal','telegram','handle'))"
        ),
        "value_normalized": "text NOT NULL DEFAULT ''",
        "value_raw": "text NOT NULL DEFAULT ''",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    # M7's one generic work queue (server/jobs/queue.py) -- one table
    # across every job type, a deliberate deviation from CATO's one-
    # queue-table-per-job-type pattern: CATO's own two pipelines are
    # high-volume and homogeneous, this platform's job types are
    # low-volume and heterogeneous, so a shared table avoids a schema
    # migration per new job type. Unscoped (see UNSCOPED_TABLES above).
    "jobs.work_queue": {
        "id": _UUID_PK,
        "job_type": "text NOT NULL DEFAULT ''",
        "payload": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "status": (
            "text NOT NULL DEFAULT 'pending' "
            "CHECK (status IN ('pending','claimed','done','failed','dead_letter'))"
        ),
        "claimed_by": "text NOT NULL DEFAULT ''",
        "claimed_at": "timestamptz",
        "attempts": "integer NOT NULL DEFAULT 0",
        "max_attempts": "integer NOT NULL DEFAULT 5",
        # A failed item becomes reclaimable only once its backoff has
        # elapsed -- also doubles as "not yet due" scheduling for a
        # future enqueue-for-later caller, at no extra cost.
        "available_at": "timestamptz NOT NULL DEFAULT now()",
        "last_error": "text NOT NULL DEFAULT ''",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    },

    # Heartbeat/liveness only (CATO's own dist_fetch/worker_lib.py
    # finding, ported: this table does not drive claiming, claim_batch()
    # never joins against it). worker_id embeds hostname+pid so a
    # restart is a new identity; reaped past a stale-heartbeat threshold
    # on every worker startup, same as CATO.
    "jobs.workers": {
        "worker_id": "text PRIMARY KEY",
        "status": "text NOT NULL DEFAULT 'running'",
        "last_heartbeat": _CREATED,
        "started_at": _CREATED,
        "stop_requested": "boolean NOT NULL DEFAULT false",
    },

    # M7's pgvector search. `owner_type`/`owner_id` names the record this
    # chunk was derived from (e.g. "interaction"); `content_text` is the
    # chunk itself, kept alongside the vector so a search result can show
    # a snippet without a second fetch. `visibility_user_id` is safety
    # rule 10, non-negotiable: null for org-wide content, set to the
    # source interaction's owner for anything body-derived -- retrieval
    # (server/api/search.py) filters on this, or semantic search quietly
    # becomes a way to read other people's mail. Unique on
    # (org_id, owner_type, owner_id, chunk_kind, content_hash) makes a
    # re-embed idempotent; a model change is a backfill, not a migration.
    "ai.embeddings": {
        "id": _UUID_PK,
        "org_id": _ORG_FK,
        "owner_type": "text NOT NULL DEFAULT ''",
        "owner_id": "uuid NOT NULL",
        "visibility_user_id": "uuid REFERENCES core.users(id) ON DELETE CASCADE",
        "chunk_kind": "text NOT NULL DEFAULT ''",
        "chunk_index": "integer NOT NULL DEFAULT 0",
        "content_hash": "text NOT NULL DEFAULT ''",
        "content_text": "text NOT NULL DEFAULT ''",
        "model": "text NOT NULL DEFAULT ''",
        "dim": "integer NOT NULL DEFAULT 0",
        "embedding": f"vector({EMBEDDING_DIM})",
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

    # ── Record entities ──────────────────────────────────────────────────
    # Default list order is (updated_at DESC, id DESC): `id` is always appended
    # so the sort is a total order. Without it, keyset pagination silently
    # duplicates and skips rows, which reads as a data bug rather than a paging
    # bug.
    "CREATE INDEX IF NOT EXISTS ix_organizations_org_updated "
    "ON core.organizations (org_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_organizations_org_owner "
    "ON core.organizations (org_id, owner_id)",
    "CREATE INDEX IF NOT EXISTS ix_organizations_org_name "
    "ON core.organizations (org_id, name_normalized)",
    "CREATE INDEX IF NOT EXISTS ix_organizations_org_domain "
    "ON core.organizations (org_id, domain) WHERE domain <> ''",

    "CREATE INDEX IF NOT EXISTS ix_persons_org_updated "
    "ON core.persons (org_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_persons_org_owner ON core.persons (org_id, owner_id)",
    "CREATE INDEX IF NOT EXISTS ix_persons_org_name "
    "ON core.persons (org_id, name_normalized)",

    "CREATE INDEX IF NOT EXISTS ix_pipelines_org_updated "
    "ON core.pipelines (org_id, updated_at DESC, id DESC)",

    "CREATE INDEX IF NOT EXISTS ix_deals_org_updated "
    "ON core.deals (org_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_deals_org_owner ON core.deals (org_id, owner_id)",
    "CREATE INDEX IF NOT EXISTS ix_deals_org_stage "
    "ON core.deals (org_id, pipeline_id, stage)",

    "CREATE INDEX IF NOT EXISTS ix_tasks_org_updated "
    "ON core.tasks (org_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_org_owner_status "
    "ON core.tasks (org_id, owner_id, status, due_on)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_org_subject "
    "ON core.tasks (org_id, subject_type, subject_id)",

    "CREATE INDEX IF NOT EXISTS ix_notes_org_updated "
    "ON core.notes (org_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_notes_org_subject "
    "ON core.notes (org_id, subject_type, subject_id)",

    # Both directions are indexed because the edge is stored once and read from
    # either end. The partial index serves the common "current relationships"
    # case without scanning history.
    "CREATE INDEX IF NOT EXISTS ix_assoc_from "
    "ON core.associations (org_id, from_type, from_id, role)",
    "CREATE INDEX IF NOT EXISTS ix_assoc_to "
    "ON core.associations (org_id, to_type, to_id, role)",
    "CREATE INDEX IF NOT EXISTS ix_assoc_from_current "
    "ON core.associations (org_id, from_type, from_id, role) WHERE valid_to IS NULL",
    # COALESCE on valid_from lets the same pair hold successive stints (two
    # separate employments) while still rejecting an exact duplicate.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_assoc ON core.associations "
    "(org_id, from_type, from_id, role, to_type, to_id, "
    " COALESCE(valid_from, '-infinity'::date))",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_custom_fields "
    "ON core.custom_fields (org_id, entity, key) WHERE archived_at IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_custom_fields_pending "
    "ON core.custom_fields (org_id, index_state) WHERE index_state <> 'none'",

    "CREATE INDEX IF NOT EXISTS ix_saved_views_org_entity "
    "ON core.saved_views (org_id, entity, name)",

    # The two queries the closing gate and the general "documents on this
    # record" view actually run.
    "CREATE INDEX IF NOT EXISTS ix_documents_org_subject "
    "ON core.documents (org_id, subject_type, subject_id)",
    "CREATE INDEX IF NOT EXISTS ix_documents_org_status "
    "ON core.documents (org_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_documents_org_updated "
    "ON core.documents (org_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_documents_org_owner "
    "ON core.documents (org_id, owner_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_provider_envelope "
    "ON core.documents (org_id, provider, provider_envelope_id) "
    "WHERE provider_envelope_id <> ''",

    "CREATE INDEX IF NOT EXISTS ix_document_signers_org_document "
    "ON core.document_signers (org_id, document_id, order_index)",

    "CREATE INDEX IF NOT EXISTS ix_interactions_org_thread "
    "ON core.interactions (org_id, thread_id)",
    "CREATE INDEX IF NOT EXISTS ix_interactions_org_occurred "
    "ON core.interactions (org_id, occurred_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_interactions_org_owner "
    "ON core.interactions (org_id, owner_id)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_channels_org_kind_value "
    "ON core.contact_channels (org_id, kind, value_normalized)",
    "CREATE INDEX IF NOT EXISTS ix_contact_channels_org_person "
    "ON core.contact_channels (org_id, person_id)",

    # Uniqueness makes `interactions.py`'s own participant-recording
    # idempotent (ON CONFLICT DO NOTHING) the same way re-processing the
    # same interaction already is for contact derivation. The second index
    # is the actual query a person's merged timeline runs: every
    # interaction_id a given person participated in.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_interaction_participants "
    "ON core.interaction_participants (org_id, interaction_id, person_id, role)",
    "CREATE INDEX IF NOT EXISTS ix_interaction_participants_org_person "
    "ON core.interaction_participants (org_id, person_id)",

    "CREATE INDEX IF NOT EXISTS ix_metric_facts_org_subject_key "
    "ON core.metric_facts (org_id, subject_type, subject_id, metric_key)",
    "CREATE INDEX IF NOT EXISTS ix_metric_facts_org_period "
    "ON core.metric_facts (org_id, period_start, period_end)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_settings_org_section "
    "ON core.settings (org_id, section)",

    "CREATE INDEX IF NOT EXISTS ix_events_org_record "
    "ON core.events (org_id, entity, record_id, seq DESC)",
    "CREATE INDEX IF NOT EXISTS ix_events_undelivered "
    "ON core.events (org_id, seq) WHERE delivery_state = 'pending'",

    # Containment and equality over custom fields. Range queries need a
    # promoted expression index instead -- see server/core/custom_fields.py.
    # Single-column, so verify_index_leading_org() (which inspects indnatts > 1)
    # correctly ignores these.
    "CREATE INDEX IF NOT EXISTS ix_organizations_custom_gin "
    "ON core.organizations USING gin (custom jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_persons_custom_gin "
    "ON core.persons USING gin (custom jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_deals_custom_gin "
    "ON core.deals USING gin (custom jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_custom_gin "
    "ON core.tasks USING gin (custom jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_notes_custom_gin "
    "ON core.notes USING gin (custom jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_documents_custom_gin "
    "ON core.documents USING gin (custom jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_interactions_custom_gin "
    "ON core.interactions USING gin (custom jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_contact_channels_custom_gin "
    "ON core.contact_channels USING gin (custom jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_metric_facts_custom_gin "
    "ON core.metric_facts USING gin (custom jsonb_path_ops)",

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

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_connected_accounts_org_user_provider "
    "ON core.connected_accounts (org_id, user_id, provider)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_account_credentials_account "
    "ON core.account_credentials (org_id, account_id)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_oauth_pending_state "
    "ON core.oauth_pending (org_id, state)",
    "CREATE INDEX IF NOT EXISTS ix_oauth_pending_created "
    "ON core.oauth_pending (created_at)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sync_cursors_account_resource "
    "ON core.sync_cursors (org_id, account_id, resource)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sync_links_account_object "
    "ON core.sync_links (org_id, account_id, provider_object_id)",
    "CREATE INDEX IF NOT EXISTS ix_sync_links_org_entity "
    "ON core.sync_links (org_id, entity_type, entity_id)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_trusted_senders_org_pattern "
    "ON core.trusted_senders (org_id, pattern)",

    "CREATE INDEX IF NOT EXISTS ix_proposed_changes_org_subject "
    "ON core.proposed_changes (org_id, subject_type, subject_id)",
    "CREATE INDEX IF NOT EXISTS ix_proposed_changes_org_status "
    "ON core.proposed_changes (org_id, status)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_channels_org_kind_value "
    "ON core.user_channels (org_id, kind, value_normalized)",
    "CREATE INDEX IF NOT EXISTS ix_user_channels_org_user "
    "ON core.user_channels (org_id, user_id)",

    # jobs.work_queue is unscoped (see UNSCOPED_TABLES) -- claimed across
    # every org by one generic worker pool, so it has no org_id to lead
    # with. This is CATO's own measured finding (~5400x on this exact
    # claim-query shape): without (status, available_at) leading the
    # index, the claim query's ORDER BY forces a full scan.
    "CREATE INDEX IF NOT EXISTS ix_work_queue_status_available "
    "ON jobs.work_queue (status, available_at)",
    "CREATE INDEX IF NOT EXISTS ix_work_queue_job_type_status "
    "ON jobs.work_queue (job_type, status)",

    "CREATE UNIQUE INDEX IF NOT EXISTS uq_embeddings_org_owner_chunk "
    "ON ai.embeddings (org_id, owner_type, owner_id, chunk_kind, content_hash)",
    "CREATE INDEX IF NOT EXISTS ix_embeddings_org_owner "
    "ON ai.embeddings (org_id, owner_type, owner_id)",
    # Single-column (the vector itself), so verify_index_leading_org()
    # correctly ignores it -- same reasoning as the custom-fields GIN
    # indexes above. Cosine distance: the standard choice for both
    # providers' sentence-embedding models.
    "CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw_cosine "
    "ON ai.embeddings USING hnsw (embedding vector_cosine_ops)",

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


# ── Cast helpers for custom-field indexes ────────────────────────────────────
#
# A raw `((custom->>'renewal')::date)` expression index is a multi-tenant
# landmine. The index is on the whole table, so once org A promotes `renewal` as
# a date, org B storing "whenever" in a key of the same name makes **org B's
# INSERT fail outright** -- one tenant's schema choice takes another tenant's
# writes down, and the error names a cast nobody in org B has heard of.
#
# These helpers degrade an unparseable value to NULL instead. They must be
# IMMUTABLE to be indexable, which `pg_input_is_valid()` (PG16+) allows without
# an exception block -- a plpgsql BEGIN/EXCEPTION function cannot be IMMUTABLE
# in a way the planner will trust for an index.
#
# server/core/query.py builds ORDER BY and WHERE from these same functions, so
# the filter expression is textually identical to the index expression. That is
# the only reason a promoted index actually gets used.
#
# NOT a validation path: `to_date('2026-13-45','YYYY-MM-DD')` silently returns
# 2027-02-14 rather than raising. Type validation is the repository's job, where
# a ValueError triggers the same repair-retry as everywhere else (safety rule 6).
FUNCTIONS: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION core.jsonb_num(j jsonb, k text) RETURNS numeric
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $fn$
      SELECT CASE WHEN pg_input_is_valid(j ->> k, 'numeric')
                  THEN (j ->> k)::numeric END
    $fn$
    """,
    """
    CREATE OR REPLACE FUNCTION core.jsonb_date(j jsonb, k text) RETURNS date
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $fn$
      SELECT CASE WHEN pg_input_is_valid(j ->> k, 'date')
                  THEN (j ->> k)::date END
    $fn$
    """,
    """
    CREATE OR REPLACE FUNCTION core.jsonb_ts(j jsonb, k text) RETURNS timestamptz
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $fn$
      SELECT CASE WHEN pg_input_is_valid(j ->> k, 'timestamptz')
                  THEN (j ->> k)::timestamptz END
    $fn$
    """,
    """
    CREATE OR REPLACE FUNCTION core.jsonb_bool(j jsonb, k text) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $fn$
      SELECT CASE WHEN pg_input_is_valid(j ->> k, 'boolean')
                  THEN (j ->> k)::boolean END
    $fn$
    """,
    """
    CREATE OR REPLACE FUNCTION core.jsonb_text(j jsonb, k text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $fn$
      SELECT j ->> k
    $fn$
    """,
)

# ── Views ────────────────────────────────────────────────────────────────────
#
# One stored row per relationship, readable from both ends. `security_invoker`
# is load-bearing: a view runs as its OWNER by default, which is the classic way
# an RLS boundary gets bypassed. FORCE ROW LEVEL SECURITY on the base table
# happens to save us here, but relying on that is an accident rather than a
# design -- so the invoker semantics are stated, and tests/test_rls.py asserts a
# second org sees zero rows through the view.
#
# The self-loop exclusion on the second branch is the only thing stopping a
# person-to-person association from being counted twice.
VIEWS: tuple[str, ...] = (
    """
    CREATE OR REPLACE VIEW core.association_edges WITH (security_invoker = true) AS
    SELECT id, org_id, from_type AS self_type, from_id AS self_id,
           to_type AS other_type, to_id AS other_id, role, 'out'::text AS direction,
           attributes, valid_from, valid_to, created_at
      FROM core.associations
    UNION ALL
    SELECT id, org_id, to_type, to_id, from_type, from_id, role, 'in'::text,
           attributes, valid_from, valid_to, created_at
      FROM core.associations
     WHERE NOT (from_type = to_type AND from_id = to_id)
    """,
)


# ── Module-contributed schema ────────────────────────────────────────────────
#
# A module registers its own tables and indexes through here. Core never names
# them (R6) -- it only knows that some tables came from somewhere else, and they
# go through exactly the same migration, RLS and verification path as core's.
#
# Module tables get the standard org_id predicate. A module cannot opt out of
# tenant isolation: UNSCOPED_TABLES is core's alone, and the two identity tables
# in it are the only deliberate holes in the boundary.

_MODULE_TABLES: dict[str, dict[str, str]] = {}
_MODULE_INDEXES: list[str] = []


def register_module_schema(
    tables: dict[str, dict[str, str]], indexes: Iterable[str] = ()
) -> None:
    """Contribute tables and indexes from a module. Idempotent."""
    for name, columns in tables.items():
        if name in TABLES:
            raise ValueError(f"{name} is a core table and cannot be redefined")
        _MODULE_TABLES[name] = columns
    for statement in indexes:
        if statement not in _MODULE_INDEXES:
            _MODULE_INDEXES.append(statement)


def reset_module_schema() -> None:
    """Drop module registrations. Tests only."""
    _MODULE_TABLES.clear()
    _MODULE_INDEXES.clear()


def all_tables() -> dict[str, dict[str, str]]:
    """Core tables followed by module tables. Order matters: module tables
    reference core ones, and _ensure_table creates in insertion order."""
    return {**TABLES, **_MODULE_TABLES}


def predicate_for(table: str) -> str:
    return RLS_PREDICATES.get(table, _ORG_PREDICATE)


# ── Migration ────────────────────────────────────────────────────────────────

def _pk_clause(table: str, columns: dict[str, str]) -> str:
    pk_name, pk_decl = next(iter(columns.items()))
    return f"CREATE TABLE IF NOT EXISTS {table} ({pk_name} {pk_decl})"


class VectorExtensionMissing(RuntimeError):
    """`CREATE EXTENSION vector` requires a superuser -- the app's own role
    deliberately never is one (see this module's own docstring and safety
    rule: "the app's database role must never have BYPASSRLS and must
    never be a superuser"). A DBA runs `CREATE EXTENSION vector;` once, as
    a superuser, before first boot -- the same one-time bootstrap category
    as creating the database and the app role itself."""


def _ensure_schemas(cur) -> None:
    for name in SCHEMAS:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {name}")


def _ensure_vector_extension(cur) -> None:
    """Read-check-then-create, not a bare `CREATE EXTENSION IF NOT EXISTS`
    -- an already-installed extension must never even attempt the
    superuser-only path, or a deployment that already did the one-time
    DBA step would still fail to boot under the app's own (correctly
    unprivileged) role."""
    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
    if cur.fetchone() is not None:
        return
    try:
        cur.execute("CREATE EXTENSION vector")
    except psycopg2.errors.InsufficientPrivilege as exc:
        raise VectorExtensionMissing(
            "the 'vector' extension (pgvector) is not installed, and this "
            "role correctly cannot create it. Ask a DBA to run "
            "`CREATE EXTENSION vector;` once, as a superuser, against this "
            "database, then restart."
        ) from exc


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
    every = all_tables()
    wanted = list(tables) if tables is not None else list(every)
    with pool.system_transaction() as cur:
        _ensure_schemas(cur)
        _ensure_vector_extension(cur)
        # Functions precede indexes: the promoted custom-field indexes in
        # server/core/custom_fields.py are expressions over them.
        for statement in FUNCTIONS:
            cur.execute(statement)
        for table in wanted:
            _ensure_table(cur, table, every[table])
        for statement in list(INDEXES) + _MODULE_INDEXES:
            cur.execute(statement)
        for statement in VIEWS:
            cur.execute(statement)
        for table in wanted:
            if table in UNSCOPED_TABLES:
                continue
            _ensure_rls(cur, table, predicate_for(table))


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

    for table in all_tables():
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
    allowed = {
        "uq_sessions_token", "uq_orgs_slug",
        # jobs.work_queue is unscoped (UNSCOPED_TABLES) -- claimed across
        # every org by one generic worker pool, so it has no org_id
        # column to lead with at all.
        "ix_work_queue_status_available", "ix_work_queue_job_type_status",
    }
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

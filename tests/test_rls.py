"""Tenant isolation.

These are the tests M0 exists for. Each one fails against a plausible-looking
misconfiguration that every functional test in the suite would still pass:
policies enabled but not FORCEd, a connection used without context, or an index
whose leading column is not org_id.
"""
from __future__ import annotations

import pytest

from server.core import users
from server.db import pool, schema


def test_every_table_has_rls_enabled_forced_and_policied():
    """The three-part check. FORCE especially: without it the table owner --
    which is the app role here -- bypasses every policy, and nothing else in
    the suite would notice."""
    assert schema.verify_rls() == []


def test_app_role_cannot_bypass_rls():
    """A superuser or BYPASSRLS app role silently defeats the entire schema."""
    health = pool.healthcheck()
    assert health["is_superuser"] is False
    assert health["bypasses_rls"] is False
    assert health["rls_enforced"] is True


def test_composite_indexes_lead_with_org_id():
    """Not correctness but a documented two-orders-of-magnitude cliff: an RLS
    predicate on a non-leading index column cannot be used efficiently."""
    assert schema.verify_index_leading_org() == []


def test_org_scoped_query_sees_only_its_own_rows():
    a = users.create_org("Org A")
    b = users.create_org("Org B")
    users.create_user(str(a["id"]), email="a@example.com", password="correct-horse-battery")
    users.create_user(str(b["id"]), email="b@example.com", password="correct-horse-battery")

    assert [u["email"] for u in users.list_users(str(a["id"]))] == ["a@example.com"]
    assert [u["email"] for u in users.list_users(str(b["id"]))] == ["b@example.com"]


def test_cross_tenant_read_by_id_returns_nothing():
    """Knowing another tenant's primary key must not be enough."""
    a = users.create_org("Org A")
    b = users.create_org("Org B")
    victim = users.create_user(
        str(a["id"]), email="a@example.com", password="correct-horse-battery"
    )
    assert users.get_user(str(b["id"]), str(victim["id"])) is None


def test_connection_without_context_sees_nothing():
    """The fail-closed default. An uncontexted connection must return an empty
    result, never the whole table -- this is what NULLIF(...)::uuid buys."""
    org = users.create_org("Org A")
    users.create_user(str(org["id"]), email="a@example.com", password="correct-horse-battery")

    with pool.system_transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM core.users")
        assert int(cur.fetchone()["n"]) == 0


def test_cross_tenant_write_is_rejected():
    """WITH CHECK, not just USING: a tenant must not be able to insert a row
    stamped with somebody else's org_id."""
    a = users.create_org("Org A")
    b = users.create_org("Org B")

    import psycopg2

    with pytest.raises(psycopg2.Error):
        with pool.transaction(org_id=str(a["id"])) as cur:
            cur.execute(
                "INSERT INTO core.users (org_id, email) VALUES (%s, %s)",
                (str(b["id"]), "smuggled@example.com"),
            )


def test_set_local_context_does_not_leak_between_transactions():
    """SET LOCAL is transaction-scoped. A plain SET would persist on the
    physical connection and leak tenant context to the next borrower -- a
    cross-tenant leak that only appears under connection reuse."""
    org = users.create_org("Org A")

    with pool.transaction(org_id=str(org["id"])) as cur:
        assert pool.current_context(cur)["org_id"] == str(org["id"])

    with pool.system_transaction() as cur:
        assert pool.current_context(cur)["org_id"] is None


def test_every_scoped_table_has_a_policy_predicate():
    """A new table added to TABLES without an entry in RLS_PREDICATES would
    otherwise ship unprotected. Opting out is possible, but only by naming the
    table in UNSCOPED_TABLES, where it has to be justified in writing."""
    assert set(schema.RLS_PREDICATES) == set(schema.TABLES) - schema.UNSCOPED_TABLES


def test_the_unscoped_set_is_exactly_the_identity_layer():
    """A tripwire on the one deliberate hole in tenant isolation. Adding a
    table here must be a conscious, reviewed act -- not something that happens
    because a policy was inconvenient."""
    assert schema.UNSCOPED_TABLES == {"auth.identities", "auth.sessions"}


def test_identity_tables_carry_no_credentials_or_profile():
    """The auth schema is outside RLS, so its contents are the blast radius of
    that decision. It must hold routing information only -- no password hash,
    no name, no profile."""
    forbidden = {"password_hash", "name", "email_raw", "custom"}
    for table in schema.UNSCOPED_TABLES:
        assert not (set(schema.TABLES[table]) & forbidden), table

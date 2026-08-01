"""Test fixtures.

Runs against a real PostgreSQL database, not a mock. The whole point of M0 is
row-level security, `FORCE ROW LEVEL SECURITY`, `SET LOCAL` context and index
shape — none of which a fake driver can tell you anything about. A test suite
that mocked psycopg2 here would pass while tenant isolation was completely
broken.

Point it at a throwaway database:

    CRM_TEST_DB_NAME=crm_test CRM_DB_USER=crm_app CRM_DB_PASSWORD=... pytest

Isolation is by truncation between tests rather than a rollback fixture,
because the code under test manages its own transactions through the pool.
"""
from __future__ import annotations

import os

import pytest

# Point the config singleton at the test database BEFORE anything imports it.
os.environ.setdefault("CRM_DB_NAME", os.environ.get("CRM_TEST_DB_NAME", "crm_test"))
os.environ.setdefault("CRM_DB_USER", "crm_app")
os.environ.setdefault("CRM_DB_PASSWORD", "crm_dev")
os.environ.setdefault("CRM_DB_HOST", "localhost")
os.environ.setdefault("CRM_SESSION_COOKIE_SECURE", "false")

from server import config  # noqa: E402
from server.core import events, modules, permissions, registry, users  # noqa: E402
from server.db import pool, schema  # noqa: E402


def _database_available() -> bool:
    try:
        pool.healthcheck()
        return True
    except Exception:  # noqa: BLE001 -- any failure means "no database"
        return False


@pytest.fixture(scope="session", autouse=True)
def _migrated() -> None:
    if not _database_available():
        pytest.skip(
            f"no PostgreSQL at {config.get_db_dsn()!r} -- see tests/conftest.py",
            allow_module_level=True,
        )
    registry.reset()
    schema.reset_module_schema()
    # The same loader server/api/app.py's lifespan uses, driven by
    # config.get_enabled_modules() -- one implementation of "register core,
    # then let every enabled module install" (R1), not a second copy of it
    # here that could drift from what a real deployment actually boots.
    modules.install_enabled_modules()
    schema.migrate()
    yield
    pool.close_pool()


@pytest.fixture(autouse=True)
def _no_leftover_validators():
    """Validators are global, like events subscribers -- but unlike
    subscribers, some are installed once per session by a module and must
    survive every test, so a blanket reset would delete them permanently."""
    snapshot = registry.validator_snapshot()
    yield
    registry.restore_validators(snapshot)


@pytest.fixture(autouse=True)
def _no_leftover_subscribers():
    """Subscribers are global. A test that registers one must not affect the
    next, or failures appear in whichever test happens to run after it."""
    yield
    events.reset()


@pytest.fixture
def principal(admin) -> permissions.Principal:
    return permissions.load_principal(admin)


@pytest.fixture
def member_principal(member) -> permissions.Principal:
    return permissions.load_principal(member)


@pytest.fixture(autouse=True)
def _clean_tables(_migrated) -> None:
    """Empty every table between tests.

    CASCADE follows the foreign keys, and truncating core.orgs therefore clears
    everything beneath it — but every table is listed explicitly anyway, so a
    future table that is not reachable from orgs cannot silently leak state
    from one test into the next.
    """
    yield
    with pool.system_transaction() as cur:
        tables = ", ".join(reversed(list(schema.all_tables())))
        cur.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


@pytest.fixture
def org() -> dict:
    return users.create_org("Test Org", "test-org")


@pytest.fixture
def org_id(org) -> str:
    return str(org["id"])


@pytest.fixture
def admin(org_id) -> dict:
    return users.create_user(
        org_id,
        email="admin@example.com",
        name="Admin",
        password="correct-horse-battery",
        is_admin=True,
    )


@pytest.fixture
def member(org_id) -> dict:
    return users.create_user(
        org_id,
        email="member@example.com",
        name="Member",
        password="correct-horse-battery",
        is_admin=False,
    )


@pytest.fixture
def client(_migrated):
    """A TestClient with the app's lifespan run, so startup migration and the
    RLS verification actually execute in tests."""
    from fastapi.testclient import TestClient

    from server.api.app import app

    with TestClient(app) as test_client:
        yield test_client

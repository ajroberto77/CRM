"""THE connection pool (R5), and the only place tenant context is set.

Every query in the platform borrows a connection from here inside a
`with transaction(...)` block. There is no second pool, no module-level
connection, and no code path that opens its own psycopg2 connection.

## Why the context manager is the only API

RLS policies read `current_setting('app.org_id')`. If a connection is used
without that setting, every policy evaluates against an empty string and the
query returns nothing -- a silent empty result, not an error. Making the
context manager the only way to get a cursor means a caller cannot forget.

## SET LOCAL, never SET

Connections may run through a transaction-mode pooler, where a plain `SET`
persists on the physical connection after the transaction ends and leaks tenant
context to the next borrower -- a cross-tenant data leak that appears only under
concurrency. `SET LOCAL` is scoped to the transaction and reverts on commit or
rollback. This file uses SET LOCAL unconditionally, including when no pooler is
configured, because the failure mode is severe and the cost is zero.

## Parameterization

`SET LOCAL` does not accept a bind parameter, so the value is passed through
`set_config(name, value, true)`, which does. Never build a SET LOCAL string by
interpolation.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

from server import config

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()

# Postgres GUCs the RLS policies and the repository layer read. Namespaced
# under `app.` so they cannot collide with a real setting.
ORG_GUC = "app.org_id"
USER_GUC = "app.user_id"


class PoolError(RuntimeError):
    pass


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """The singleton pool, created on first use."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            minconn, maxconn = config.get_db_pool_bounds()
            try:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn, maxconn, dsn=config.get_db_dsn()
                )
            except psycopg2.Error as exc:
                raise PoolError(f"could not create connection pool: {exc}") from exc
    return _pool


def close_pool() -> None:
    """Close every pooled connection. Used at shutdown and between test
    modules; safe to call when no pool exists."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


@contextmanager
def _borrow() -> Iterator[psycopg2.extensions.connection]:
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        # Never hand back a connection mid-transaction: the next borrower would
        # inherit an open transaction and, worse, whatever SET LOCAL context it
        # carried.
        try:
            if conn.status != psycopg2.extensions.STATUS_READY:
                conn.rollback()
        finally:
            pool.putconn(conn)


@contextmanager
def transaction(
    *,
    org_id: Optional[str] = None,
    user_id: Optional[str] = None,
    readonly: bool = False,
) -> Iterator[psycopg2.extensions.cursor]:
    """Yield a RealDictCursor inside one transaction, with tenant context set.

    Commits on clean exit, rolls back on any exception, and always returns the
    connection to the pool.

        with pool.transaction(org_id=org, user_id=user) as cur:
            cur.execute("SELECT ... FROM core.persons WHERE ...", (value,))
            rows = cur.fetchall()

    `org_id=None` runs with no tenant context. Under RLS that means the query
    sees nothing, which is the correct default -- it is NOT an admin escape.
    Schema migration and the first-run bootstrap are the only legitimate
    context-free callers, and they run as the owner role via
    `system_transaction()` below.
    """
    with _borrow() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            if readonly:
                cur.execute("SET LOCAL transaction_read_only = on")
            # set_config(..., is_local => true) is the parameterizable form of
            # SET LOCAL. Values are passed as bind params, never interpolated.
            if org_id is not None:
                cur.execute("SELECT set_config(%s, %s, true)", (ORG_GUC, str(org_id)))
            if user_id is not None:
                cur.execute("SELECT set_config(%s, %s, true)", (USER_GUC, str(user_id)))
            yield cur
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            cur.close()


@contextmanager
def system_transaction() -> Iterator[psycopg2.extensions.cursor]:
    """A transaction with no tenant context, for schema migration and the
    first-run bootstrap only.

    This is not a privilege escalation: it sets no GUC, so under RLS it sees no
    tenant rows. It exists to make context-free access explicit and greppable,
    rather than having callers pass org_id=None and look identical to a bug.
    Anything that reads or writes tenant data must use transaction().
    """
    with _borrow() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            cur.close()


@contextmanager
def ddl_connection(
    *,
    lock_timeout_ms: int = 3000,
    statement_timeout_ms: int = 0,
) -> Iterator[psycopg2.extensions.cursor]:
    """An AUTOCOMMIT connection, for `CREATE/DROP INDEX CONCURRENTLY` only.

    This exists because `CONCURRENTLY` cannot run inside a transaction block,
    and both `transaction()` and `system_transaction()` open one. It is not a
    second pool (R5) -- it borrows from the same one and restores the
    connection's isolation level on the way out.

    `lock_timeout` matters more than it looks. `CREATE INDEX CONCURRENTLY` does
    two table scans and then waits for every concurrent transaction to drain; a
    single `idle in transaction` connection stalls it indefinitely. A timeout
    turns that into a recorded failure the promoter can retry, rather than a
    hung worker nobody notices.

    `statement_timeout` defaults to 0 (off) because a legitimate index build on
    a large table can take minutes and must not be killed halfway -- a failed
    CONCURRENTLY build leaves an INVALID index behind.
    """
    with _borrow() as conn:
        previous = conn.isolation_level
        conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(f"SET lock_timeout = {int(lock_timeout_ms)}")
            cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
            yield cur
        finally:
            try:
                cur.execute("SET lock_timeout = DEFAULT")
                cur.execute("SET statement_timeout = DEFAULT")
            except psycopg2.Error:
                pass  # a dead connection is about to be discarded anyway
            cur.close()
            conn.set_isolation_level(previous)


def current_context(cur: psycopg2.extensions.cursor) -> dict[str, Any]:
    """The tenant context actually in force on this cursor. Used by tests and
    by the RLS assertions; reads the GUCs rather than trusting the caller's
    intent. `current_setting(..., true)` returns NULL instead of erroring when
    the GUC was never set."""
    cur.execute(
        "SELECT current_setting(%s, true) AS org_id, current_setting(%s, true) AS user_id",
        (ORG_GUC, USER_GUC),
    )
    row = cur.fetchone() or {}
    return {"org_id": row.get("org_id") or None, "user_id": row.get("user_id") or None}


# Features this codebase depends on and the version that provides them:
#   15 -- security_invoker views (core.association_edges must not bypass RLS)
#   16 -- pg_input_is_valid(), for IMMUTABLE cast helpers with no exception block
# Asserted at startup rather than discovered when a view silently returns
# another tenant's rows.
MIN_SERVER_VERSION = 160000


def healthcheck() -> dict[str, Any]:
    """Liveness for GET /health. Reports whether the app role is dangerously
    privileged -- an app role with BYPASSRLS or superuser silently defeats every
    policy in the schema, so this is surfaced rather than assumed."""
    with system_transaction() as cur:
        cur.execute(
            "SELECT current_database() AS database, current_user AS role, "
            "       version() AS server_version, "
            "       current_setting('server_version_num')::int AS server_version_num, "
            "       (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_superuser, "
            "       (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) AS bypasses_rls"
        )
        row = dict(cur.fetchone())
    row["rls_enforced"] = not (row.get("is_superuser") or row.get("bypasses_rls"))
    row["version_supported"] = row["server_version_num"] >= MIN_SERVER_VERSION
    return row

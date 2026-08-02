"""Shared helpers for the standalone poller processes in this package
(`sync_poller.py`, `message_poller.py`) -- each is its own `while True:
poll_once(); sleep()` loop (Cal's `poll_loop.py` pattern), so the one
thing genuinely common between them, listing every org to poll, lives
here once rather than being copied into each (R1).
"""
from __future__ import annotations

from server.db import pool


def all_org_ids() -> list[str]:
    """Every org with at least one user -- i.e. every org that can
    actually own a connected account or a linked channel to poll for.

    Deliberately reads `auth.identities`, not `core.orgs`: `core.orgs`
    carries RLS on its own `id` (`server/db/schema.py`'s
    `_ORG_SELF_PREDICATE`), and `pool.system_transaction()` sets no
    tenant GUC at all -- under `FORCE ROW LEVEL SECURITY` that predicate
    evaluates false for every row, so a plain `SELECT id FROM core.orgs`
    here would silently return nothing, always, for every org, forever.
    `auth.identities` is the one table genuinely outside the RLS boundary
    (it is what establishes tenant context in the first place, per its
    own docstring in `schema.py`), so it is the only legitimate place a
    context-free query can discover which orgs exist.
    """
    with pool.system_transaction() as cur:
        cur.execute("SELECT DISTINCT org_id FROM auth.identities")
        return [str(row["org_id"]) for row in cur.fetchall()]

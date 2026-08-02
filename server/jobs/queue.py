"""M7's one generic work queue (`jobs.work_queue`) -- one table across
every job type, a deliberate deviation from CATO's one-queue-table-per-
job-type pattern (`dist_fetch/worker_lib.py`): CATO's own two pipelines
are high-volume and homogeneous (a fixed, known Form4/13F shape), this
platform's job types are low-volume and heterogeneous, so a shared table
avoids a schema migration per new job type.

Claim/backoff mechanics are otherwise ported directly from CATO's
`claim_batch()`: `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP
LOCKED) AND status = 'pending'` -- the outer redundant status check is
defense-in-depth, kept. The `(status, available_at)` composite index this
relies on is CATO's own measured finding (~5400x on this exact query
shape), not a micro-optimization.

`jobs.work_queue`/`jobs.workers` are UNSCOPED (see
`server/db/schema.py`'s `UNSCOPED_TABLES`): a single generic worker pool
claims a batch across every org in one query, so this module always uses
`pool.system_transaction()`/a raw connection, never `pool.transaction(org_id=...)`.
Any job whose work touches real tenant data carries its own `org_id` key
inside `payload`; the handler that processes it opens its own
org-scoped transaction to do that work -- this module never does.

One automatic addition beyond CATO's own manual-only `retry_failed`:
once `attempts >= max_attempts`, `mark_failed()` moves the row to
`dead_letter` instead of leaving it reclaimable, since these jobs run
unattended (no human watching a CLI to notice "retry_failed" needs
running).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from server.db import pool

STATUSES = ("pending", "claimed", "done", "failed", "dead_letter")
_DEFAULT_MAX_ATTEMPTS = 5
# Exponential backoff, capped -- a job that keeps failing shouldn't be
# reclaimed every second, but also shouldn't wait an unbounded amount of
# time once it's genuinely recovered.
_BACKOFF_CAP_SECONDS = 600


def _backoff_seconds(attempts: int) -> int:
    return min(_BACKOFF_CAP_SECONDS, 2**attempts)


def enqueue(
    job_type: str, payload: dict[str, Any], *, max_attempts: int = _DEFAULT_MAX_ATTEMPTS
) -> dict[str, Any]:
    with pool.system_transaction() as cur:
        cur.execute(
            "INSERT INTO jobs.work_queue (job_type, payload, max_attempts) "
            "VALUES (%s, %s::jsonb, %s) RETURNING *",
            (job_type, json.dumps(payload, default=str), max_attempts),
        )
        return dict(cur.fetchone())


def claim_batch(worker_id: str, job_types: tuple[str, ...], batch_size: int) -> list[dict[str, Any]]:
    """Atomically claim up to `batch_size` pending, due (`available_at <=
    now()`) rows of the given job types, transitioning them to
    'claimed'. `job_types` is never empty -- a worker always knows what
    it can handle."""
    if not job_types:
        return []
    with pool.system_transaction() as cur:
        cur.execute(
            "UPDATE jobs.work_queue SET status = 'claimed', claimed_by = %s, "
            "claimed_at = now(), updated_at = now() "
            "WHERE id IN ("
            "  SELECT id FROM jobs.work_queue "
            "  WHERE status = 'pending' AND job_type = ANY(%s) AND available_at <= now() "
            "  ORDER BY available_at "
            "  LIMIT %s "
            "  FOR UPDATE SKIP LOCKED"
            ") "
            "AND status = 'pending' "
            "RETURNING *",
            (worker_id, list(job_types), batch_size),
        )
        return [dict(row) for row in cur.fetchall()]


def mark_done(job_id: str) -> None:
    with pool.system_transaction() as cur:
        cur.execute(
            "UPDATE jobs.work_queue SET status = 'done', last_error = '', updated_at = now() "
            "WHERE id = %s",
            (job_id,),
        )


def mark_failed(job_id: str, error: str) -> None:
    """Increments `attempts` and schedules the next retry via exponential
    backoff -- unless this was the last allowed attempt, in which case the
    row moves to `dead_letter` (terminal, never automatically reclaimed)
    instead of back to `pending`."""
    with pool.system_transaction() as cur:
        cur.execute(
            "SELECT attempts, max_attempts FROM jobs.work_queue WHERE id = %s", (job_id,)
        )
        row = cur.fetchone()
        if row is None:
            return
        attempts = row["attempts"] + 1
        detail = str(error)[:500]
        if attempts >= row["max_attempts"]:
            cur.execute(
                "UPDATE jobs.work_queue SET status = 'dead_letter', attempts = %s, "
                "last_error = %s, updated_at = now() WHERE id = %s",
                (attempts, detail, job_id),
            )
            return
        cur.execute(
            "UPDATE jobs.work_queue SET status = 'pending', attempts = %s, last_error = %s, "
            "claimed_by = '', claimed_at = NULL, "
            "available_at = now() + (%s || ' seconds')::interval, updated_at = now() "
            "WHERE id = %s",
            (attempts, detail, _backoff_seconds(attempts), job_id),
        )


def reclaim_stale_claims(stale_minutes: int) -> int:
    """A claimed row whose worker crashed mid-job (never called
    `mark_done`/`mark_failed`) is reset back to `pending` after
    `stale_minutes` -- regardless of which worker claimed it, unlike a
    worker recovering its own claims on restart. Returns the number of
    rows reset."""
    with pool.system_transaction() as cur:
        cur.execute(
            "UPDATE jobs.work_queue SET status = 'pending', claimed_by = '', "
            "claimed_at = NULL, updated_at = now() "
            "WHERE status = 'claimed' "
            "AND claimed_at < now() - (%s || ' minutes')::interval "
            "RETURNING id",
            (stale_minutes,),
        )
        return len(cur.fetchall())


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    with pool.system_transaction() as cur:
        cur.execute("SELECT * FROM jobs.work_queue WHERE id = %s", (job_id,))
        row = cur.fetchone()
        return dict(row) if row else None


# ── Worker registration/heartbeat ───────────────────────────────────────────

_STALE_WORKER_MINUTES = 10


def reap_stale_workers(stale_minutes: int = _STALE_WORKER_MINUTES) -> int:
    """Deletes worker rows whose heartbeat has gone silent for
    `stale_minutes` -- `worker_id` embeds hostname+pid, so a restart is a
    new identity and a crashed worker's row would otherwise linger
    forever. Called from `register_worker()`, same as CATO's own
    `worker_lib.py` -- no separate daemon/cron needed."""
    with pool.system_transaction() as cur:
        cur.execute(
            "DELETE FROM jobs.workers WHERE last_heartbeat < now() - (%s || ' minutes')::interval",
            (stale_minutes,),
        )
        return cur.rowcount


def register_worker(worker_id: str) -> None:
    reap_stale_workers()
    with pool.system_transaction() as cur:
        cur.execute(
            "INSERT INTO jobs.workers (worker_id, status, last_heartbeat, started_at) "
            "VALUES (%s, 'running', now(), now()) "
            "ON CONFLICT (worker_id) DO UPDATE SET "
            "status = 'running', last_heartbeat = now(), started_at = now(), stop_requested = false",
            (worker_id,),
        )


def heartbeat(worker_id: str) -> None:
    with pool.system_transaction() as cur:
        cur.execute(
            "UPDATE jobs.workers SET last_heartbeat = now(), status = 'running' "
            "WHERE worker_id = %s",
            (worker_id,),
        )


def check_stop_requested(worker_id: str) -> bool:
    with pool.system_transaction() as cur:
        cur.execute(
            "SELECT stop_requested FROM jobs.workers WHERE worker_id = %s", (worker_id,)
        )
        row = cur.fetchone()
        return bool(row and row["stop_requested"])


def mark_worker_stopped(worker_id: str) -> None:
    with pool.system_transaction() as cur:
        cur.execute(
            "UPDATE jobs.workers SET status = 'stopped', last_heartbeat = now() "
            "WHERE worker_id = %s",
            (worker_id,),
        )

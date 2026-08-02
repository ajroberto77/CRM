"""The generic worker loop over `jobs.work_queue` -- claims a batch,
dispatches each item by `job_type` to a handler registered in a small
in-process registry, heartbeats between batches. `sync_poller.py` and
`message_poller.py` are NOT rewritten onto this queue (lower risk; their
small, fixed-cardinality polling doesn't need general queue machinery) --
this serves only the new M7 job types (event redelivery, embeddings).

Run as its own process: `python3 -m server.jobs.workers`.
"""
from __future__ import annotations

import os
import socket
import time
from typing import Any, Callable

from server.jobs import queue

_HEARTBEAT_INTERVAL_SECONDS = 15
_STALE_CLAIM_MINUTES = 45
_DEFAULT_BATCH_SIZE = 10
_DEFAULT_POLL_INTERVAL_SECONDS = 5
# Spaced well beyond normal job-processing latency -- see
# event_redelivery.py's own docstring on why a short interval here risks
# enqueuing duplicate (wasteful, never incorrect) redelivery attempts.
_EVENT_SWEEP_INTERVAL_SECONDS = 120

_HANDLERS: dict[str, Callable[[dict[str, Any]], None]] = {}


def register_handler(job_type: str, handler: Callable[[dict[str, Any]], None]) -> None:
    """Registers `handler(payload)` for `job_type`. Idempotent -- re-
    registering the same job_type just replaces the handler, same
    convention `server/core/events.py`'s subscriber registration
    already uses."""
    _HANDLERS[job_type] = handler


def reset_handlers() -> None:
    """Test-only: clears every registered handler. Mirrors
    `server/core/registry.py`'s `reset()` for the same reason -- a clean
    slate between test modules that each want to register their own
    fakes."""
    _HANDLERS.clear()


def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def run_once(worker_id: str, *, batch_size: int = _DEFAULT_BATCH_SIZE) -> int:
    """Claims and processes one batch. Returns the number of jobs
    claimed (whether they succeeded or failed)."""
    job_types = tuple(_HANDLERS)
    if not job_types:
        return 0
    claimed = queue.claim_batch(worker_id, job_types, batch_size)
    for job in claimed:
        handler = _HANDLERS.get(job["job_type"])
        if handler is None:
            # Can only happen if a job_type was claimed here that no
            # handler in THIS process knows about -- e.g. an older
            # deploy still running mid-rollout claimed a newer job_type.
            # Not a job to keep retrying blindly: dead_letter immediately
            # by exhausting attempts, since no amount of backoff makes an
            # unregistered handler appear.
            queue.mark_failed(str(job["id"]), f"no handler registered for {job['job_type']!r}")
            continue
        try:
            handler(job["payload"])
        except Exception as exc:  # noqa: BLE001 -- a handler's own failure must not crash the worker loop
            queue.mark_failed(str(job["id"]), str(exc))
        else:
            queue.mark_done(str(job["id"]))
    return len(claimed)


def run_forever(
    *, batch_size: int = _DEFAULT_BATCH_SIZE,
    poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL_SECONDS,
    sweep_interval_seconds: int = _EVENT_SWEEP_INTERVAL_SECONDS,
) -> None:
    worker_id = _worker_id()
    queue.register_worker(worker_id)
    last_heartbeat = 0.0
    last_sweep = 0.0
    try:
        while True:
            if queue.check_stop_requested(worker_id):
                break
            queue.reclaim_stale_claims(_STALE_CLAIM_MINUTES)
            now = time.time()
            if now - last_sweep >= sweep_interval_seconds:
                # Local import: event_redelivery pulls in server.core.events,
                # which this generic queue module otherwise knows nothing
                # about -- keeps the sweep an opt-in call site, not a
                # module-level dependency every worker process pays for.
                from server.jobs import event_redelivery
                event_redelivery.sweep()
                last_sweep = now
            claimed = run_once(worker_id, batch_size=batch_size)
            now = time.time()
            if now - last_heartbeat >= _HEARTBEAT_INTERVAL_SECONDS:
                queue.heartbeat(worker_id)
                last_heartbeat = now
            if not claimed:
                time.sleep(poll_interval_seconds)
    finally:
        queue.mark_worker_stopped(worker_id)


if __name__ == "__main__":
    from server.jobs import handlers
    handlers.register_all()
    run_forever()

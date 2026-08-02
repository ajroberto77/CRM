"""M7's event redelivery job type -- drains `server/core/events.py`'s
outbox for at-least-once delivery, per that module's own documented
design ("an M7 worker can drain `delivery_state = 'pending'` ... without
any subscriber changing").

Two halves:

- `sweep()` finds every undelivered event across every org (reusing
  `server/jobs/__init__.py`'s `all_org_ids()`, the same cross-tenant
  discovery M6's message poller needs) and enqueues one `redeliver_event`
  job per event. Run periodically from `server/jobs/workers.py`'s own
  loop -- not itself a queued job, since `jobs.work_queue` has no
  recurring-job concept, and a periodic in-process call is the simplest
  correct shape for a first cut.
- `handle_redeliver_event()` is the registered handler for that job
  type: a thin wrapper around `events.redeliver()`, the actual
  reconstruct-and-redispatch logic, which lives in `events.py` itself
  (R1 -- redelivery is the event bus's own capability, not a second
  implementation of it here).

Known tradeoff, not hidden: `sweep()` does not check whether a
`redeliver_event` job for a given event id is already queued before
enqueuing another one, so a short sweep interval relative to processing
latency could enqueue more than one redelivery attempt for the same
event before the first is processed. `events.py`'s own docstring already
requires subscribers to be "idempotent and commutative," so a duplicate
redelivery is wasteful, never incorrect -- and `workers.py`'s default
sweep interval is spaced well beyond normal processing latency to make
it uncommon in practice.
"""
from __future__ import annotations

from typing import Any

from server import jobs
from server.core import events
from server.jobs import queue

_DEFAULT_GRACE_SECONDS = 300

JOB_TYPE = "redeliver_event"


def sweep(*, grace_seconds: int = _DEFAULT_GRACE_SECONDS) -> int:
    """Enqueues one `redeliver_event` job per undelivered event across
    every org. Returns the number enqueued."""
    enqueued = 0
    for org_id in jobs.all_org_ids():
        for row in events.find_undelivered(org_id, grace_seconds):
            queue.enqueue(JOB_TYPE, {"org_id": org_id, "event_id": str(row["id"])})
            enqueued += 1
    return enqueued


def handle_redeliver_event(payload: dict[str, Any]) -> None:
    events.redeliver(payload["org_id"], payload["event_id"])

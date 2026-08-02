"""Subscribes to `interaction` CREATE and enqueues an `embed_interaction`
job (M7's work queue) so the body gets chunked and embedded for search --
the same event-bus subscriber pattern `derivation.py`/`deal_activity.py`
already use, not a bespoke trigger.

The actual embedding work (a real network round-trip to an LLM provider)
happens in the queued job (`server/jobs/embed_interaction.py`), not
inline here: `publish_committed()` runs subscribers synchronously right
after the triggering write commits, and a slow/failing provider call
must never hold up or fail an unrelated interaction write -- exactly the
reasoning M5's own scheduling-extraction subscriber accepts already, but
that one is a same-process LLM call the platform already tolerates being
synchronous; embeddings additionally fan out per chunk, so queuing keeps
the write path's latency independent of body length.
"""
from __future__ import annotations

from server.core import embeddings, events
from server.jobs import queue

JOB_TYPE = "embed_interaction"


def _on_interaction_created(event: events.RecordEvent) -> None:
    interaction = event.after
    if not interaction:
        return
    body = (interaction.get("body") or "").strip()
    if not body:
        return
    queue.enqueue(JOB_TYPE, {
        "org_id": event.org_id,
        "interaction_id": str(interaction["id"]),
        "owner_id": str(interaction["owner_id"]),
    })


def _on_interaction_deleted(event: events.RecordEvent) -> None:
    """A deleted interaction's stored body text goes with it -- an
    orphaned embedding would keep a deleted email's content searchable
    (and its snippet readable in a search result) after the user deleted
    the record it came from. DB-only cleanup, so this runs inline rather
    than through the job queue (unlike the CREATE path, there's no
    network call here to keep off the synchronous dispatch path)."""
    if not event.record_id:
        return
    embeddings.delete_for_owner(event.org_id, "interaction", str(event.record_id))


def install() -> None:
    events.subscribe(
        "core.enqueue_interaction_embedding", _on_interaction_created,
        entities=("interaction",), actions=(events.CREATED,),
    )
    events.subscribe(
        "core.delete_interaction_embeddings", _on_interaction_deleted,
        entities=("interaction",), actions=(events.DELETED,),
    )

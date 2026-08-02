"""Registers every known `job_type` with `server/jobs/workers.py`'s
in-process handler registry. One place, mirroring
`server/core/modules.py`'s `install_enabled_modules()` shape: a single
idempotent entrypoint, so `python -m server.jobs.workers` and the test
suite both wire up the exact same set of handlers rather than two
slightly different copies drifting apart.
"""
from __future__ import annotations

from server.core import interaction_embeddings
from server.jobs import embed_interaction, event_redelivery, workers


def register_all() -> None:
    """Idempotent -- registering twice just replaces each handler with
    itself. Filled in by each job-type producer as it's added."""
    workers.register_handler(event_redelivery.JOB_TYPE, event_redelivery.handle_redeliver_event)
    workers.register_handler(
        interaction_embeddings.JOB_TYPE, embed_interaction.handle_embed_interaction
    )

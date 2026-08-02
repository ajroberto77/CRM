"""The `embed_interaction` job handler -- enqueued by
`server/core/interaction_embeddings.py`'s subscriber, registered here
with `server/jobs/workers.py`'s in-process handler registry.

Re-fetches the interaction (it may have since been deleted -- a purged
subject is a no-op, not a failure), chunks its body, embeds each chunk
(`server/llm/embeddings.py`), and stores it (`server/core/embeddings.py`)
with `visibility_user_id` set to the interaction's own owner -- safety
rule 10, non-negotiable: this is body-derived content, so it is owner-
scoped, not org-wide, at the exact same point every other body-derived
write in this platform already scopes it.
"""
from __future__ import annotations

from typing import Any

from server.core import embeddings, permissions, repository
from server.llm import embeddings as embedding_provider


def handle_embed_interaction(payload: dict[str, Any]) -> None:
    org_id = payload["org_id"]
    interaction_id = payload["interaction_id"]
    owner_id = payload["owner_id"]

    principal = permissions.system_principal(org_id, "embed interaction")
    try:
        interaction = repository.get_record(principal, "interaction", interaction_id)
    except repository.NotFound:
        return  # deleted since this was enqueued -- nothing to embed

    body = (interaction.get("body") or "").strip()
    if not body:
        return

    for index, chunk in enumerate(embeddings.chunk_text(body)):
        vector, model = embedding_provider.embed(org_id, chunk)
        embeddings.store_chunk(
            org_id, owner_type="interaction", owner_id=interaction_id,
            visibility_user_id=owner_id, chunk_kind="body", chunk_index=index,
            content_text=chunk, vector=vector, model=model,
        )

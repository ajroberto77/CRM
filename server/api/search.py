"""POST /search -- M7's pgvector semantic search over `ai.embeddings`.

Not routed through `records.py`'s generic entity CRUD: `ai.embeddings`
isn't a registered entity at all (see `server/core/embeddings.py`'s own
docstring), and a search query embeds the QUERY text itself before doing
a nearest-neighbor lookup -- there's no "list records matching a filter"
shape here, just "rank chunks by similarity to this text."

Safety rule 10 is enforced one layer down, in `embeddings.search()`
itself (`visibility_user_id IS NULL OR = principal.user_id`) -- this
route only ever passes the CALLING principal's own org_id/user_id, never
anything from the request body.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from server.api.auth import current_principal
from server.core import embeddings
from server.core.permissions import Principal
from server.llm import embeddings as embedding_provider

router = APIRouter(prefix="/search", tags=["search"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=50)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "owner_type": row["owner_type"],
        "owner_id": str(row["owner_id"]),
        "chunk_kind": row["chunk_kind"],
        "content_text": row["content_text"],
        "distance": float(row["distance"]),
    }


@router.post("")
def search(body: SearchRequest, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    vector, _model = embedding_provider.embed(principal.org_id, body.query)
    results = embeddings.search(principal.org_id, principal.user_id, vector, limit=body.limit)
    return {"results": [_public(r) for r in results]}

"""Storage for `ai.embeddings` -- M7's pgvector search. Deliberately NOT
a registered R4 entity, same reasoning as `server/core/accounts.py`'s
`connected_accounts`: a raw embedding vector is never something a
generic create/update form should construct by hand, and safety rule
10's `visibility_user_id` scoping needs to be enforced at the one place
that reads this table, not trusted to the generic own/team field-mask
model. Every write and read goes through the functions here.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from server.db import pool

# A simple fixed-size chunker for a first cut -- no sentence/paragraph
# awareness, just a character window. Good enough to keep each stored
# chunk well under a typical embedding model's input limit; a smarter
# splitter is a follow-up, not a correctness requirement here.
_CHUNK_CHARS = 2000


def chunk_text(text: str, *, chunk_chars: int = _CHUNK_CHARS) -> list[str]:
    text = text.strip()
    if not text:
        return []
    return [text[i:i + chunk_chars] for i in range(0, len(text), chunk_chars)]


def _vector_literal(vector: list[float]) -> str:
    """pgvector's own text input format, `[v1,v2,...]` -- built by hand
    rather than via `pgvector.psycopg2.register_vector()`'s adapter
    registration, matching this codebase's "psycopg2, explicit cursor
    pattern, no wrapper abstractions" convention."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def store_chunk(
    org_id: str, *, owner_type: str, owner_id: str,
    visibility_user_id: Optional[str], chunk_kind: str, chunk_index: int,
    content_text: str, vector: list[float], model: str,
) -> dict[str, Any]:
    """Idempotent re-embed: the same (owner, chunk_kind, content) upserts
    in place rather than duplicating (`uq_embeddings_org_owner_chunk`) --
    a model change is a backfill that overwrites the stored vector, not a
    migration."""
    content_hash = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "INSERT INTO ai.embeddings "
            "(org_id, owner_type, owner_id, visibility_user_id, chunk_kind, "
            " chunk_index, content_hash, content_text, model, dim, embedding) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector) "
            "ON CONFLICT (org_id, owner_type, owner_id, chunk_kind, content_hash) "
            "DO UPDATE SET visibility_user_id = EXCLUDED.visibility_user_id, "
            "  model = EXCLUDED.model, dim = EXCLUDED.dim, embedding = EXCLUDED.embedding "
            "RETURNING *",
            (
                org_id, owner_type, owner_id, visibility_user_id, chunk_kind,
                chunk_index, content_hash, content_text, model, len(vector),
                _vector_literal(vector),
            ),
        )
        return dict(cur.fetchone())


def delete_for_owner(org_id: str, owner_type: str, owner_id: str) -> None:
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "DELETE FROM ai.embeddings WHERE org_id = %s AND owner_type = %s AND owner_id = %s",
            (org_id, owner_type, owner_id),
        )


def search(org_id: str, user_id: str, query_vector: list[float], *, limit: int = 10) -> list[dict[str, Any]]:
    """Nearest-neighbor search, safety-rule-10-filtered: a row is visible
    only when it's org-wide (`visibility_user_id IS NULL`) or belongs to
    the calling user. This filter is the entire point of the rule --
    `tests/test_embeddings.py` asserts directly that a different user's
    body-derived embedding never comes back, regardless of similarity."""
    with pool.transaction(org_id=org_id, user_id=user_id, readonly=True) as cur:
        literal = _vector_literal(query_vector)
        cur.execute(
            "SELECT id, owner_type, owner_id, chunk_kind, content_text, "
            "       embedding <=> %s::vector AS distance "
            "FROM ai.embeddings "
            "WHERE org_id = %s AND (visibility_user_id IS NULL OR visibility_user_id = %s) "
            "ORDER BY embedding <=> %s::vector "
            "LIMIT %s",
            (literal, org_id, user_id, literal, limit),
        )
        return [dict(row) for row in cur.fetchall()]

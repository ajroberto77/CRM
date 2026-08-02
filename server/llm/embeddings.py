"""Text embeddings -- a different API shape than `router.py`'s chat-
completion contract (one vector out, not a completion), so this is not
shoehorned into that module. Dispatches to whichever provider is
currently active in the org's `"llm"` settings section (M3's
`server/core/settings.py`), reusing `server/net/http_retry.py` for the
HTTP mechanics, same as every other provider adapter in this codebase.

Only ollama and openai support embeddings today -- the other three
configured chat providers (anthropic, gemini, claudecode) have no
embeddings endpoint this platform calls; `embed()` raises clearly rather
than silently falling back to a provider the org never chose.

## The fixed dimension

`ai.embeddings.embedding` is `vector(server.db.schema.EMBEDDING_DIM)` --
pgvector's HNSW index requires every indexed vector to share one
dimension, so this is a single platform-wide choice. `EMBEDDING_DIM`
(768) matches Ollama's default embedding model (`nomic-embed-text`);
OpenAI's v3 embedding models support a `dimensions` parameter that
truncates their native output to a requested size without materially
harming retrieval quality (OpenAI's own documented feature), so the
OpenAI call below explicitly requests `EMBEDDING_DIM` too -- both
providers write into the same column shape. `embed()` verifies the
returned vector's length before returning it (safety rule 6's "validate
every field" applied here): a misconfigured Ollama embedding model that
doesn't happen to be 768-dimensional fails loudly at the call site,
not as a cryptic pgvector insert error two layers away.
"""
from __future__ import annotations

import json
from typing import Any

from server import config
from server.core import settings
from server.db.schema import EMBEDDING_DIM
from server.net import http_retry

_DEFAULT_OLLAMA_EMBEDDING_MODEL = "nomic-embed-text"
_DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"


class EmbeddingError(RuntimeError):
    pass


def _ollama_embed(text: str, section: dict[str, Any]) -> tuple[list[float], str]:
    host = section.get("ollama_host") or "localhost"
    port = section.get("ollama_port") or "11434"
    model = section.get("ollama_embedding_model") or _DEFAULT_OLLAMA_EMBEDDING_MODEL
    try:
        result = http_retry.request_with_retry(
            "POST", f"http://{host}:{port}/api/embeddings",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"model": model, "prompt": text}).encode("utf-8"),
            max_retries=2,
        )
    except http_retry.HTTPRetryError as exc:
        raise EmbeddingError(f"ollama embeddings request failed: {exc}") from exc
    embedding = result.get("embedding")
    if not embedding:
        raise EmbeddingError("ollama embeddings response had no 'embedding' field")
    return embedding, model


def _openai_embed(text: str, section: dict[str, Any]) -> tuple[list[float], str]:
    api_key = config.get_secret("openai_api_key")
    if not api_key:
        raise EmbeddingError("OPENAI_API_KEY is not set")
    model = section.get("openai_embedding_model") or _DEFAULT_OPENAI_EMBEDDING_MODEL
    payload = {"model": model, "input": text, "dimensions": EMBEDDING_DIM}
    try:
        result = http_retry.request_with_retry(
            "POST", "https://api.openai.com/v1/embeddings",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            data=json.dumps(payload).encode("utf-8"),
            max_retries=2,
        )
    except http_retry.HTTPRetryError as exc:
        raise EmbeddingError(f"openai embeddings request failed: {exc}") from exc
    data = result.get("data") or []
    if not data:
        raise EmbeddingError("openai embeddings response had no 'data'")
    return data[0]["embedding"], model


def embed(org_id: str, text: str) -> tuple[list[float], str]:
    """Embeds `text` via the org's currently active LLM provider. Returns
    `(vector, model)` -- the model name is stored alongside the vector
    (`ai.embeddings.model`) so a future re-embed knows what produced the
    stored value. Raises `EmbeddingError` for an unsupported provider, a
    provider failure, or a vector whose length doesn't match
    `EMBEDDING_DIM`."""
    section = settings.get_settings(org_id, "llm")
    provider = section.get("provider", "ollama")
    if provider == "ollama":
        vector, model = _ollama_embed(text, section)
    elif provider == "openai":
        vector, model = _openai_embed(text, section)
    else:
        raise EmbeddingError(
            f"embeddings are not supported for provider {provider!r} "
            "(known: ollama, openai)"
        )
    if len(vector) != EMBEDDING_DIM:
        raise EmbeddingError(
            f"{provider} model {model!r} returned a {len(vector)}-dimensional "
            f"vector, expected {EMBEDDING_DIM}"
        )
    return vector, model

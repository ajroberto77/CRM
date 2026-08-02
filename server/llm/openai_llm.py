"""OpenAI adapter.

## Structured output

Real strict-mode JSON Schema: `call_structured()` sends `response_format:
{"type": "json_schema", "json_schema": {"strict": true, "schema": ...}}` --
OpenAI enforces the shape itself (Cal's shape; JA's classify.py only used
the weaker `{"type": "json_object"}` mode, which guarantees valid JSON but
not the schema's actual shape).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from server import config
from server.llm.router import ProviderUnavailable, classify_http_error
from server.net import http_retry

_BASE_URL = "https://api.openai.com/v1"


def _require_key() -> str:
    key = config.get_secret("openai_api_key")
    if not key:
        raise ProviderUnavailable("OPENAI_API_KEY not set")
    return key


def _post(payload: dict[str, Any]) -> dict[str, Any]:
    key = _require_key()
    try:
        return http_retry.request_with_retry(
            "POST", f"{_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            data=json.dumps(payload).encode("utf-8"), max_retries=3,
        )
    except http_retry.HTTPRetryError as exc:
        raise classify_http_error(exc) from exc


def call(
    prompt: str, *, model: str, settings: dict[str, Any],
    want_json: bool = False, temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature if temperature is not None else 0.2,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if want_json:
        payload["response_format"] = {"type": "json_object"}
    result = _post(payload)
    return result["choices"][0]["message"]["content"]


def call_structured(
    prompt: str, schema: dict[str, Any], *, model: str, settings: dict[str, Any],
    temperature: Optional[float] = None, max_tokens: Optional[int] = None,
) -> Any:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature if temperature is not None else 0.2,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "strict": True, "schema": schema},
        },
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    result = _post(payload)
    content = result["choices"][0]["message"]["content"]
    return json.loads(content)


def list_models(settings: dict[str, Any]) -> list[str]:
    key = _require_key()
    try:
        result = http_retry.request_with_retry(
            "GET", f"{_BASE_URL}/models", headers={"Authorization": f"Bearer {key}"},
            max_retries=2,
        )
    except http_retry.HTTPRetryError as exc:
        raise classify_http_error(exc) from exc
    prefixes = ("gpt-", "o1", "o3", "o4")
    return sorted(
        m["id"] for m in result.get("data", []) if m["id"].startswith(prefixes)
    )

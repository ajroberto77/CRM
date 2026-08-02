"""Anthropic (Claude, direct API) adapter.

## Structured output

No native JSON-Schema response mode on this API -- `call_structured()` uses
forced tool-use instead (`tool_choice: {"type": "tool", "name": ...}`),
Cal's exact mechanism: the schema is declared as a tool's input schema, the
model is forced to call it, and the parsed tool input IS the structured
result. This is Anthropic's own documented pattern for schema-constrained
output, not a workaround.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from server import config
from server.llm.router import ProviderUnavailable, classify_http_error
from server.net import http_retry

_BASE_URL = "https://api.anthropic.com/v1"
_API_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 2048


def _require_key() -> str:
    key = config.get_secret("anthropic_api_key")
    if not key:
        raise ProviderUnavailable("ANTHROPIC_API_KEY not set")
    return key


def _headers(key: str) -> dict[str, str]:
    return {
        "x-api-key": key,
        "anthropic-version": _API_VERSION,
        "Content-Type": "application/json",
    }


def _post(payload: dict[str, Any]) -> dict[str, Any]:
    key = _require_key()
    try:
        return http_retry.request_with_retry(
            "POST", f"{_BASE_URL}/messages", headers=_headers(key),
            data=json.dumps(payload).encode("utf-8"), max_retries=3,
        )
    except http_retry.HTTPRetryError as exc:
        raise classify_http_error(exc) from exc


def call(
    prompt: str, *, model: str, settings: dict[str, Any],
    want_json: bool = False, temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    payload = {
        "model": model,
        "max_tokens": max_tokens if max_tokens is not None else _DEFAULT_MAX_TOKENS,
        "temperature": temperature if temperature is not None else 0.2,
        "messages": [{"role": "user", "content": prompt}],
    }
    result = _post(payload)
    return result["content"][0]["text"]


def call_structured(
    prompt: str, schema: dict[str, Any], *, model: str, settings: dict[str, Any],
    temperature: Optional[float] = None, max_tokens: Optional[int] = None,
) -> Any:
    payload = {
        "model": model,
        "max_tokens": max_tokens if max_tokens is not None else _DEFAULT_MAX_TOKENS,
        "temperature": temperature if temperature is not None else 0.2,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{"name": "extract", "description": "Extract structured data.", "input_schema": schema}],
        "tool_choice": {"type": "tool", "name": "extract"},
    }
    result = _post(payload)
    for block in result.get("content", []):
        if block.get("type") == "tool_use":
            return block["input"]
    raise ValueError("no tool_use block in Claude's response")


def list_models(settings: dict[str, Any]) -> list[str]:
    key = _require_key()
    try:
        result = http_retry.request_with_retry(
            "GET", f"{_BASE_URL}/models", headers=_headers(key), max_retries=2,
        )
    except http_retry.HTTPRetryError as exc:
        raise classify_http_error(exc) from exc
    return [m["id"] for m in result.get("data", [])]

"""Gemini adapter.

Auth is a URL query param (`?key=...`), not a header -- the one provider
here shaped that way. Structured output sets `responseMimeType:
"application/json"` + `responseSchema: schema` in `generationConfig`, the
same real schema-constrained mode Cal uses.
"""
from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import quote

from server import config
from server.llm.router import ProviderUnavailable, classify_http_error
from server.net import http_retry

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def _require_key() -> str:
    key = config.get_secret("gemini_api_key")
    if not key:
        raise ProviderUnavailable("GEMINI_API_KEY not set")
    return key


def _endpoint(model: str, method: str) -> str:
    key = _require_key()
    return f"{_BASE_URL}/models/{model}:{method}?key={quote(key)}"


def _post(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return http_retry.request_with_retry(
            "POST", _endpoint(model, "generateContent"),
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload).encode("utf-8"), max_retries=3,
        )
    except http_retry.HTTPRetryError as exc:
        raise classify_http_error(exc) from exc


def _text_of(result: dict[str, Any]) -> str:
    candidates = result.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini returned no candidates")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    if not parts:
        raise ValueError("Gemini returned an empty candidate")
    return parts[0].get("text", "")


def call(
    prompt: str, *, model: str, settings: dict[str, Any],
    want_json: bool = False, temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    gen_cfg: dict[str, Any] = {"temperature": temperature if temperature is not None else 0.2}
    if max_tokens is not None:
        gen_cfg["maxOutputTokens"] = max_tokens
    if want_json:
        gen_cfg["responseMimeType"] = "application/json"
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_cfg}
    result = _post(model, payload)
    return _text_of(result)


def call_structured(
    prompt: str, schema: dict[str, Any], *, model: str, settings: dict[str, Any],
    temperature: Optional[float] = None, max_tokens: Optional[int] = None,
) -> Any:
    gen_cfg: dict[str, Any] = {
        "temperature": temperature if temperature is not None else 0.2,
        "responseMimeType": "application/json",
        "responseSchema": schema,
    }
    if max_tokens is not None:
        gen_cfg["maxOutputTokens"] = max_tokens
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_cfg}
    result = _post(model, payload)
    return json.loads(_text_of(result))


def list_models(settings: dict[str, Any]) -> list[str]:
    key = _require_key()
    try:
        result = http_retry.request_with_retry(
            "GET", f"{_BASE_URL}/models?key={quote(key)}", headers={}, max_retries=2,
        )
    except http_retry.HTTPRetryError as exc:
        raise classify_http_error(exc) from exc
    return [
        m["name"].removeprefix("models/")
        for m in result.get("models", [])
        if "generateContent" in (m.get("supportedGenerationMethods") or [])
    ]

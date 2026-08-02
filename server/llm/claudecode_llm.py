"""Claude Code (self-hosted bridge, `claude-api-bridge`) adapter.

Async request/poll, not request/response: `POST /api/ask` returns a
`requestId`, then `GET /api/ask/{id}` is polled every 2s (up to 300s) for
`status == "completed"`.

Always sends `Authorization: Bearer <key-or-"not-needed">` -- the real
bridge expects the header to be present even when it doesn't check the
value, so a missing `claudecode_key` secret is not treated as
`ProviderUnavailable` the way it is for the other three keyed providers.

## No native structured-output primitive

The bridge is a plain prose in/out API with no schema-constrained mode.
`call_structured()` embeds the schema as prompt text and regex-extracts the
first `{...}` object from the free-text response -- the one adapter where
"ask nicely and parse" is the only option (Cal's exact fallback for this
same bridge).
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from server import config
from server.llm.router import LLMError, ProviderUnavailable, classify_http_error
from server.net import http_retry

_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = "3456"
_POLL_INTERVAL_SECONDS = 2
_POLL_TIMEOUT_SECONDS = 300
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

MODEL_ALIASES = ("opus", "sonnet", "haiku", "fable")


def _base_url(settings: dict[str, Any]) -> str:
    host = settings.get("claudecode_host") or _DEFAULT_HOST
    port = settings.get("claudecode_port") or _DEFAULT_PORT
    return f"http://{host}:{port}"


def _headers() -> dict[str, str]:
    key = config.get_secret("claudecode_key") or "not-needed"
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _ask(prompt: str, *, model: str, settings: dict[str, Any]) -> str:
    base_url = _base_url(settings)
    try:
        submitted = http_retry.request_with_retry(
            "POST", f"{base_url}/api/ask", headers=_headers(),
            data=json.dumps({"message": prompt, "model": model}).encode("utf-8"),
            max_retries=2,
        )
    except http_retry.HTTPRetryError as exc:
        raise classify_http_error(exc) from exc

    request_id = submitted.get("requestId")
    if not request_id:
        raise LLMError("claude-api-bridge did not return a requestId")

    deadline = time.time() + _POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            polled = http_retry.request_with_retry(
                "GET", f"{base_url}/api/ask/{request_id}", headers=_headers(), max_retries=1,
            )
        except http_retry.HTTPRetryError as exc:
            raise classify_http_error(exc) from exc
        status = polled.get("status")
        if status == "completed":
            return polled.get("response", "")
        if status == "failed":
            raise LLMError(f"claude-api-bridge request {request_id} failed: {polled.get('error')}")
        time.sleep(_POLL_INTERVAL_SECONDS)

    raise ProviderUnavailable(
        f"claude-api-bridge request {request_id} did not complete within {_POLL_TIMEOUT_SECONDS}s"
    )


def call(
    prompt: str, *, model: str, settings: dict[str, Any],
    want_json: bool = False, temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    # No native JSON mode on the bridge; want_json is a prompt-level hint
    # only, honored by whatever calls in, not enforced here.
    return _ask(prompt, model=model, settings=settings)


def call_structured(
    prompt: str, schema: dict[str, Any], *, model: str, settings: dict[str, Any],
    temperature: Optional[float] = None, max_tokens: Optional[int] = None,
) -> Any:
    schema_prompt = (
        f"{prompt}\n\nRespond with ONLY a JSON object matching this schema, "
        f"nothing else:\n{json.dumps(schema)}"
    )
    text = _ask(schema_prompt, model=model, settings=settings)
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        raise ValueError(f"no JSON object found in claude-api-bridge response: {text[:200]!r}")
    return json.loads(match.group(0))


def list_models(settings: dict[str, Any]) -> list[str]:
    # No listing endpoint on the bridge -- a fixed alias list, same as the
    # settings UI presents for this provider.
    return list(MODEL_ALIASES)

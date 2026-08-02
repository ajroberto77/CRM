"""Ollama adapter -- local/self-hosted, no auth.

Ported from Cal's `extract.py` and JA's `classify.py` (both reached the
same tuning independently; JA's comments explain the "why" in the most
detail, quoted below).

## Context-window and output-length tuning

`num_ctx` caps the context window Ollama allocates for the request.
Without a cap, some models default to a much larger window than this
platform's prompts ever need, which forces partial CPU offload on a
memory-constrained GPU -- far slower per-token than staying fully on GPU.

`num_predict` bounds OUTPUT tokens. Ollama has no output ceiling by
default (unlike Claude/Gemini, which cap around 2048), so a model that
fails to stop generates until the request timeout kills it -- `format:
"json"` bounds *validity*, not *length*, so a runaway is still "valid"
JSON the whole time it never stops producing.

`keep_alive` keeps the model resident between calls. Ollama's own default
unload window is short; if calls don't arrive faster than that, every one
pays a full model-reload cost.

## Structured output

Real JSON-Schema-constrained decoding: `call_structured()` sends the
literal schema as `format` (not the string `"json"`, which is `call()`'s
loose mode) -- Ollama enforces the shape itself via grammar-constrained
sampling, so there is no prompt-embedded-schema fallback needed here,
unlike `claudecode_llm.py`.

## Wake-on-LAN

Optional, gated entirely on `settings["ollama_mac"]` being set. When
present, a sleeping host is WOKEN AND WAITED FOR, never skipped -- Ollama
is the preferred (local, free) provider, so waiting out a wake is the
right trade; rolling to a paid API just because the local box was asleep
would quietly defeat the point of running one. Bounded by
`ollama_wake_timeout` (default 90s); a wake that times out raises
`ProviderUnavailable`, which DOES let the chain roll down. A short,
per-process cooldown after a failed wake avoids hammering a host that
just failed to wake, but re-checks after it expires -- safety rule 5, no
permanent latch on recoverable external state.
"""
from __future__ import annotations

import json
import socket
import time
from typing import Any, Optional

from server.llm.router import LLMError, ProviderUnavailable, classify_http_error
from server.net import http_retry

_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = "11434"
_DEFAULT_NUM_CTX = 8192
_DEFAULT_NUM_PREDICT = 512
_DEFAULT_KEEP_ALIVE = "30m"
_DEFAULT_WAKE_TIMEOUT = 90
_WAKE_RETRY_COOLDOWN_SECONDS = 300

# Per-MAC, in-process only -- not persisted, not shared across workers. A
# restart or a different process re-checks from scratch, which is the point:
# this is a rate limit on retry attempts, not a permanent "this host is dead"
# record (safety rule 5).
_last_wake_failure: dict[str, float] = {}


def _base_url(cfg: dict[str, Any]) -> str:
    host = cfg.get("ollama_host") or _DEFAULT_HOST
    port = cfg.get("ollama_port") or _DEFAULT_PORT
    return f"http://{host}:{port}"


def _is_reachable(base_url: str) -> bool:
    try:
        http_retry.request_with_retry("GET", f"{base_url}/api/tags", headers={}, max_retries=1, timeout=3)
        return True
    except http_retry.HTTPRetryError:
        return False


def _send_magic_packet(mac: str, broadcast: str) -> None:
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + mac_bytes * 16
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, 9))
    finally:
        sock.close()


def _ensure_awake(cfg: dict[str, Any], base_url: str) -> None:
    mac = (cfg.get("ollama_mac") or "").strip()
    if not mac:
        return  # feature off -- no MAC configured
    if _is_reachable(base_url):
        return

    last_failure = _last_wake_failure.get(mac)
    if last_failure is not None and time.time() - last_failure < _WAKE_RETRY_COOLDOWN_SECONDS:
        raise ProviderUnavailable(
            f"ollama at {base_url} did not respond to a recent wake attempt "
            f"(retrying again in {int(_WAKE_RETRY_COOLDOWN_SECONDS - (time.time() - last_failure))}s)"
        )

    broadcast = cfg.get("ollama_wol_broadcast") or "255.255.255.255"
    timeout = float(cfg.get("ollama_wake_timeout") or _DEFAULT_WAKE_TIMEOUT)
    try:
        _send_magic_packet(mac, broadcast)
    except (OSError, ValueError) as exc:
        raise ProviderUnavailable(f"could not send Wake-on-LAN packet to {mac}: {exc}") from exc

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_reachable(base_url):
            _last_wake_failure.pop(mac, None)
            return
        time.sleep(2)

    _last_wake_failure[mac] = time.time()
    raise ProviderUnavailable(f"ollama at {base_url} did not wake within {timeout:.0f}s")


def _post_chat(prompt: str, *, model: str, settings: dict[str, Any],
               temperature: Optional[float], max_tokens: Optional[int],
               json_format: Any) -> dict[str, Any]:
    base_url = _base_url(settings)
    _ensure_awake(settings, base_url)

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {
            "num_ctx": int(settings.get("ollama_num_ctx") or _DEFAULT_NUM_CTX),
            "num_predict": max_tokens if max_tokens is not None
                           else int(settings.get("ollama_num_predict") or _DEFAULT_NUM_PREDICT),
            "temperature": temperature if temperature is not None else 0.2,
        },
        "keep_alive": settings.get("ollama_keep_alive") or _DEFAULT_KEEP_ALIVE,
    }
    if json_format is not None:
        payload["format"] = json_format

    try:
        return http_retry.request_with_retry(
            "POST", f"{base_url}/api/chat", headers={"Content-Type": "application/json"},
            data=json.dumps(payload).encode("utf-8"), max_retries=2,
        )
    except http_retry.HTTPRetryError as exc:
        raise classify_http_error(exc) from exc


def call(
    prompt: str, *, model: str, settings: dict[str, Any],
    want_json: bool = False, temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    result = _post_chat(
        prompt, model=model, settings=settings, temperature=temperature,
        max_tokens=max_tokens, json_format="json" if want_json else None,
    )
    return (result.get("message") or {}).get("content", "")


def call_structured(
    prompt: str, schema: dict[str, Any], *, model: str, settings: dict[str, Any],
    temperature: Optional[float] = None, max_tokens: Optional[int] = None,
) -> Any:
    result = _post_chat(
        prompt, model=model, settings=settings, temperature=temperature,
        max_tokens=max_tokens, json_format=schema,
    )
    content = (result.get("message") or {}).get("content", "")
    return json.loads(content)


def list_models(settings: dict[str, Any]) -> list[str]:
    base_url = _base_url(settings)
    try:
        result = http_retry.request_with_retry("GET", f"{base_url}/api/tags", headers={}, max_retries=2)
    except http_retry.HTTPRetryError as exc:
        raise classify_http_error(exc) from exc
    return [m["name"] for m in result.get("models", [])]

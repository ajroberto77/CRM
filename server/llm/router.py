"""LLM provider dispatch -- R3's LLM axis.

Generic core, name-prefixed adapters (R2): this module carries no provider
name and never imports a provider module except through `_import_adapter()`
below. Adding a sixth provider is one new `<provider>_llm.py` file
implementing the adapter contract, plus one `elif` there -- nothing else
changes.

## Prior art

Ported from Cal's `extract.py` (docs/SOURCE-PATTERNS.md), the more mature of
two sibling implementations researched for this axis. JA's `classify.py` has
the same five providers but no cross-provider fallback at all and no tests;
Cal has both -- a real `ProviderUnavailable`-driven fallback chain, native
structured output per provider, and a test suite (`test_llm_chain.py`)
pinning down exactly the behavior this module reproduces.

## Contract

Every adapter (`ollama_llm.py`, `openai_llm.py`, `anthropic_llm.py`,
`gemini_llm.py`, `claudecode_llm.py`) implements:

    call(prompt, *, model, settings, want_json=False,
         temperature=None, max_tokens=None) -> str
        One completion call. `want_json` requests a provider's native loose
        JSON mode, not schema-constrained. `settings` is the org's whole
        "llm" settings section (server/core/settings.py) -- most adapters
        only need `model` and read their API key from server/config.py, but
        Ollama also needs `settings["ollama_host"]`/`["ollama_port"]`, so
        every adapter gets the same section rather than a bespoke arg list.

    call_structured(prompt, schema, *, model, settings,
                     temperature=None, max_tokens=None) -> Any
        One completion call with the response constrained to `schema` by
        whatever real mechanism the provider offers -- see each adapter's
        docstring. Returns the parsed object, never a string.

    list_models(settings) -> list[str]
        A live call to the provider's model-listing endpoint, for the
        settings UI's dropdown -- `settings` is the same "llm" section
        `call()`/`call_structured()` get, since Ollama needs host/port from
        it. An adapter with no listing endpoint (claudecode) returns a
        small hardcoded fallback list instead.

Every adapter raises `ProviderUnavailable` for "could not be reached or used
at all right now" -- see `classify_http_error()`'s exact status-code table --
and lets a malformed/unparseable response surface as a plain `LLMError`,
`ValueError`, or `json.JSONDecodeError`, which the chain-aware callers below
treat as a quality problem, not an availability one.

## Fallback semantics

`ProviderUnavailable` is the ONLY exception that rolls the chain down to the
next configured provider (`extract_structured()`). A malformed response gets
exactly one repair retry on the SAME provider -- the prompt gains a hint
describing what was wrong -- then raises; it never cascades. Cascading on a
quality failure would burn a full round-trip through every configured
provider on every genuinely ambiguous input. `call_llm()` (the simpler,
non-extraction path) does not use the chain at all, matching Cal's own
task-agnostic split: it calls the org's single active provider and lets a
failure propagate.

## Settings, not env vars

Which provider(s) to use, in what order, and which model each one calls is
a per-org product setting (`server/core/settings.py`, section `"llm"`), not
process-wide config -- `server/config.py`'s `_SECRET_ENV` still holds the
API *keys* (infra-level secrets, one per deployment), but everything else
is read fresh per call, so a Settings UI change takes effect on the next
call with no restart.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from server import config
from server.core import settings
from server.net import http_retry

PROVIDERS = ("ollama", "openai", "anthropic", "gemini", "claudecode")

# Which config.py secret backs each keyed provider. Ollama has none -- it is
# local/self-hosted, configured via settings (host/port/mac), not a secret.
_SECRET_NAME = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "gemini": "gemini_api_key",
    "claudecode": "claudecode_key",
}


class LLMError(RuntimeError):
    pass


class ProviderUnavailable(LLMError):
    """The provider could not be reached or used at all right now -- host
    unreachable, key missing or invalid, quota exhausted, 5xx. Distinct from
    a plain LLMError, which means the provider answered and the answer was
    unusable. Only this exception rolls the fallback chain down to the next
    provider; see the module docstring."""


_UNAVAILABLE_STATUSES = {401, 402, 403, 408, 429}


def classify_http_error(exc: http_retry.HTTPRetryError) -> LLMError:
    """Translate a shared-retry-helper failure into this axis's own
    exception hierarchy -- every adapter's HTTP calls funnel through this.

    No response at all (`status_code is None` -- connection refused, DNS
    failure, timeout), one of the explicit `_UNAVAILABLE_STATUSES`, or any
    5xx all mean the provider is unavailable and the chain should roll
    down. Anything else (a 400 -- a malformed request WE sent) is a real
    error: rolling down would just repeat the same bad request against
    every other provider.
    """
    status = exc.status_code
    if status is None or status in _UNAVAILABLE_STATUSES or status >= 500:
        return ProviderUnavailable(str(exc))
    return LLMError(str(exc))


def _import_adapter(provider: str):
    if provider == "ollama":
        import server.llm.ollama_llm as impl
    elif provider == "openai":
        import server.llm.openai_llm as impl
    elif provider == "anthropic":
        import server.llm.anthropic_llm as impl
    elif provider == "gemini":
        import server.llm.gemini_llm as impl
    elif provider == "claudecode":
        import server.llm.claudecode_llm as impl
    else:
        raise LLMError(f"unknown LLM provider {provider!r} (known: {', '.join(PROVIDERS)})")
    return impl


# ── Chain resolution ─────────────────────────────────────────────────────────

def resolved_chain(org_id: str) -> list[dict[str, str]]:
    """The ordered list of {provider, model} to try, for this org.

    An explicitly configured chain wins. An empty one falls back to the
    single active-provider setting, synthesized into a one-entry chain --
    so a deployment that has never touched the fallback-chain editor still
    works exactly as if it had a chain of one.
    """
    section = settings.get_settings(org_id, "llm")
    chain = section.get("chain") or []
    if chain:
        return chain
    provider = section.get("provider", "ollama")
    return [{"provider": provider, "model": section.get(f"{provider}_model", "")}]


# ── Public API ───────────────────────────────────────────────────────────────

def call_llm(
    org_id: str, prompt: str, *,
    want_json: bool = False,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """The single-provider path -- no fallback chain. Matches Cal's own
    task-agnostic split: extraction work wants the chain (`extract_structured`
    below); a one-off prose call uses the org's plain active provider and
    lets a failure propagate to its caller."""
    section = settings.get_settings(org_id, "llm")
    provider = section.get("provider", "ollama")
    model = section.get(f"{provider}_model", "")
    impl = _import_adapter(provider)
    temp = temperature if temperature is not None else section.get("temperature", 0.2)
    return impl.call(
        prompt, model=model, settings=section,
        want_json=want_json, temperature=temp, max_tokens=max_tokens,
    )


def extract_structured(
    org_id: str, prompt: str, schema: dict[str, Any], *,
    validate: Optional[Callable[[Any], Any]] = None,
) -> Any:
    """The chain-aware path. A loop wrapping an inner per-provider repair --
    deliberately not self-recursive, so a malformed response never restarts
    the whole chain from the top."""
    chain = resolved_chain(org_id)
    section = settings.get_settings(org_id, "llm")
    skipped: list[str] = []
    for entry in chain:
        provider = entry.get("provider", "")
        model = entry.get("model") or section.get(f"{provider}_model", "")
        try:
            return _extract_with_repair(provider, model, section, prompt, schema, validate=validate)
        except ProviderUnavailable as exc:
            label = f"{provider}/{model}" if model else provider
            skipped.append(f"{label}: {exc}")
            continue
    raise LLMError(
        "every configured LLM provider was unavailable -- "
        + "; ".join(skipped or ["no providers configured"])
    )


def _extract_with_repair(
    provider: str, model: str, section: dict[str, Any],
    prompt: str, schema: dict[str, Any], *,
    validate: Optional[Callable[[Any], Any]] = None,
    _retry: bool = True,
) -> Any:
    impl = _import_adapter(provider)
    try:
        obj = impl.call_structured(prompt, schema, model=model, settings=section)
        return validate(obj) if validate is not None else obj
    except ProviderUnavailable:
        # Never consumed by the repair-retry: an unreachable provider can't
        # be repaired by rewording the prompt, and the chain above needs to
        # see this to roll down.
        raise
    except (LLMError, ValueError, json.JSONDecodeError) as exc:
        if not _retry:
            raise LLMError(f"structured extraction failed after repair retry: {exc}") from exc
        hint = (
            f"\n\nYour previous response could not be used: {exc}. "
            "Return valid JSON matching the schema exactly, nothing else."
        )
        return _extract_with_repair(
            provider, model, section, prompt + hint, schema,
            validate=validate, _retry=False,
        )


def list_models(org_id: str, provider: str) -> list[str]:
    impl = _import_adapter(provider)
    section = settings.get_settings(org_id, "llm")
    return impl.list_models(section)


def _probe(provider: str, model: str, section: dict[str, Any]) -> dict[str, Any]:
    """Fire one cheap real call and report success/failure -- shared by
    `check_llm_status()` (every saved provider) and `test_llm_connection()`
    (one provider, unsaved form values)."""
    impl = _import_adapter(provider)
    try:
        impl.call(
            "Reply with exactly the word: ok", model=model, settings=section,
            want_json=False, temperature=0, max_tokens=8,
        )
        return {"ok": True, "error": None}
    except LLMError as exc:
        return {"ok": False, "error": str(exc)}


def check_llm_status(org_id: str) -> dict[str, dict[str, Any]]:
    """Reachability of every provider that is actually configured (a secret
    is set, or it's Ollama, which needs none) -- one real lightweight call
    each. A provider with no secret set is reported unconfigured without a
    network call."""
    section = settings.get_settings(org_id, "llm")
    result: dict[str, dict[str, Any]] = {}
    for provider in PROVIDERS:
        secret_name = _SECRET_NAME.get(provider)
        if secret_name is not None and not config.is_secret_set(secret_name):
            result[provider] = {"ok": False, "error": "not configured"}
            continue
        model = section.get(f"{provider}_model", "")
        result[provider] = _probe(provider, model, section)
    return result


def test_llm_connection(org_id: str, overrides: dict[str, Any]) -> dict[str, Any]:
    """Fire one real call against UNSAVED form values -- never reads or
    writes `core.settings`. `overrides` names a provider plus whatever
    settings keys the form is currently showing (e.g. `{"provider":
    "openai", "openai_model": "gpt-5"}`), layered over the org's saved
    section so an in-progress edit can be tested without saving it first.
    """
    provider = overrides.get("provider")
    if not provider:
        raise LLMError("provider is required")
    section = {**settings.get_settings(org_id, "llm"), **overrides}
    model = overrides.get(f"{provider}_model") or section.get(f"{provider}_model", "")
    return _probe(provider, model, section)

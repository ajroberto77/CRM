"""E-signature dispatch -- R3's sixth provider axis.

Generic core, name-prefixed adapters (R2): this module carries no vendor name
and never imports a vendor module except through the dispatch functions below.
Adding a fifth provider is one new `<provider>_esign.py` file implementing the
same three functions, plus one `elif` here -- nothing else changes.

All four vendors John might use are wired up as options; none is the default.
The caller picks one explicitly per call (typically once per org, read from
wherever `modules/investor_portal` ends up storing that choice -- not decided
yet, see docs/INVESTOR-PORTAL.md). `core.documents.provider` records which one
actually sent a given document, so the choice is never ambiguous after the
fact even if the org's default changes later.

## Contract

Every adapter implements exactly these three functions, all synchronous:

    create_and_send(*, subject, message, document_bytes, filename, mime,
                     signers: list[SignerSpec]) -> str
        Uploads the document and sends it for signature in one logical
        operation -- some vendors need two HTTP calls to do this (PandaDoc:
        create then send once the document leaves 'uploaded' for 'draft';
        Adobe: upload a transient document then create the agreement), but
        the CALLER never sees that seam. Returns the vendor's own envelope /
        signature-request / agreement id -- store it in
        `core.documents.provider_envelope_id`.

    check_status(envelope_id: str) -> EnvelopeStatus
        Polls the vendor for current status, normalized to this module's
        closed vocabulary (see EnvelopeStatus) so `core.documents.status`
        never has to branch on which vendor sent it.

    download_executed(envelope_id: str) -> bytes
        The completed, signed PDF. Raises NotReadyError if the vendor has not
        finished producing it yet (Dropbox Sign's documented 409 for this
        exact case) -- callers should be driven by a status check or webhook
        reaching 'executed', not by calling this speculatively right after
        the last signature.

## Never live-tested

Every adapter here is written from each vendor's current REST documentation,
not exercised against a live account -- same caveat Cal's own
microsoft_calendar.py carries for its untested timezone handling. Confirm each
adapter against a real sandbox account before trusting it unattended. See each
adapter's module docstring for vendor-specific gotchas found during research.

## Retry

`_request_with_retry()` below is the one retry implementation every adapter
shares (R1) -- urllib-based, retrying on 429/503/504 and honoring
`Retry-After` where a vendor sends one, matching Cal's `http_retry.py`
shape. When the LLM router (M3) needs the same mechanics, this is what moves
into a shared `server/net/http_retry.py`; until then, one copy here is exactly
one copy, not zero and not two.
"""
from __future__ import annotations

import json as jsonlib
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

PROVIDERS = ("docusign", "dropboxsign", "pandadoc", "adobesign")

# Every vendor's real states collapse into this closed set, because
# core.documents.status (server/db/schema.py) is a fixed CHECK constraint and
# must not grow a new value every time a vendor's own vocabulary is consulted.
STATUSES = ("draft", "sent_for_signature", "partially_signed", "executed",
           "void", "expired")


class EsignError(RuntimeError):
    pass


class EsignDisabled(RuntimeError):
    """No credentials configured for this provider. Raised, never silently
    treated as 'skip this provider' -- an org that thinks e-signature is
    configured must find out immediately, not when an investor's document
    quietly never sends."""


class NotReadyError(EsignError):
    """The executed document was requested before the vendor finished
    producing it. Retry after the terminal webhook/status, not on a fixed
    delay -- see Dropbox Sign's documented 409 for this exact case."""


@dataclass(frozen=True)
class SignerSpec:
    name: str
    email: str
    role: str = "signer"
    # Routing/signing order. Equal values sign in parallel on vendors that
    # support it (DocuSign); ascending values sign in sequence on all four.
    order: int = 1


@dataclass(frozen=True)
class EnvelopeStatus:
    status: str  # one of STATUSES
    signers: tuple[dict[str, Any], ...] = ()
    raw_status: str = ""  # the vendor's own, unnormalized value -- for logs


def _validate_status(status: str) -> str:
    if status not in STATUSES:
        raise EsignError(f"adapter returned an unknown status {status!r}")
    return status


# ── Shared retry mechanics (R1 -- one implementation, all four adapters use it) ──

_RETRYABLE = (429, 500, 502, 503, 504)


def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    data: Optional[bytes] = None,
    timeout: int = 30,
    max_retries: int = 5,
    raw: bool = False,
) -> Any:
    """One HTTP call with retry on 429/5xx, honoring Retry-After when a vendor
    sends one and falling back to exponential backoff otherwise. Returns
    parsed JSON unless `raw=True`, in which case it returns the response
    bytes (for a downloaded PDF).
    """
    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return body if raw else jsonlib.loads(body.decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if exc.code == 409:
                raise NotReadyError(
                    f"{method} {url}: 409 -- document not ready yet"
                ) from exc
            if exc.code not in _RETRYABLE or attempt == max_retries - 1:
                detail = body.decode("utf-8", "replace")[:500]
                raise EsignError(f"{method} {url} failed: HTTP {exc.code}: {detail}") from exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 30)
            last_error = exc
            time.sleep(delay)
        except urllib.error.URLError as exc:
            if attempt == max_retries - 1:
                raise EsignError(f"{method} {url} failed: {exc}") from exc
            last_error = exc
            time.sleep(min(2 ** attempt, 30))
    raise EsignError(f"{method} {url} failed after {max_retries} attempts") from last_error


# ── Dispatch ─────────────────────────────────────────────────────────────────

def create_and_send(
    provider: str,
    *,
    subject: str,
    message: str,
    document_bytes: bytes,
    filename: str,
    mime: str,
    signers: list[SignerSpec],
) -> str:
    if not signers:
        raise EsignError("at least one signer is required")
    if provider == "docusign":
        import server.providers.docusign_esign as impl
    elif provider == "dropboxsign":
        import server.providers.dropboxsign_esign as impl
    elif provider == "pandadoc":
        import server.providers.pandadoc_esign as impl
    elif provider == "adobesign":
        import server.providers.adobesign_esign as impl
    else:
        raise EsignError(f"unknown esign provider {provider!r} (known: {', '.join(PROVIDERS)})")
    return impl.create_and_send(
        subject=subject, message=message, document_bytes=document_bytes,
        filename=filename, mime=mime, signers=signers,
    )


def check_status(provider: str, envelope_id: str) -> EnvelopeStatus:
    if provider == "docusign":
        import server.providers.docusign_esign as impl
    elif provider == "dropboxsign":
        import server.providers.dropboxsign_esign as impl
    elif provider == "pandadoc":
        import server.providers.pandadoc_esign as impl
    elif provider == "adobesign":
        import server.providers.adobesign_esign as impl
    else:
        raise EsignError(f"unknown esign provider {provider!r} (known: {', '.join(PROVIDERS)})")
    result = impl.check_status(envelope_id)
    _validate_status(result.status)
    return result


def download_executed(provider: str, envelope_id: str) -> bytes:
    if provider == "docusign":
        import server.providers.docusign_esign as impl
    elif provider == "dropboxsign":
        import server.providers.dropboxsign_esign as impl
    elif provider == "pandadoc":
        import server.providers.pandadoc_esign as impl
    elif provider == "adobesign":
        import server.providers.adobesign_esign as impl
    else:
        raise EsignError(f"unknown esign provider {provider!r} (known: {', '.join(PROVIDERS)})")
    return impl.download_executed(envelope_id)

"""Dropbox Sign (formerly HelloSign) API adapter.

**Never live-tested.** Written from developers.hellosign.com, not exercised
against a real account. The product is branded Dropbox Sign but the API,
SDKs and docs still say "HelloSign" throughout -- `api.hellosign.com` is the
real, current host, not a legacy fallback.

## Auth: API key via HTTP Basic

The API key is the Basic-auth username with an empty password -- simplest of
the four vendors. `_basic_auth_header()` builds this exactly once per call
rather than caching a header string, so a rotated key (this vendor supports
up to 4 keys per account) takes effect on the very next call with no restart.

## No separate sandbox host

Unlike DocuSign/Adobe (separate hosts) or PandaDoc (separate keys, same
host), Dropbox Sign toggles sandbox behavior with `test_mode: 1` on the
production endpoint itself. Test mode skips quota consumption and real
emails and watermarks output "TEST" -- but **still fires real webhooks**, so
a shared callback handler must not assume every POST it receives is a
legally binding production signature. Controlled here by
`DROPBOXSIGN_TEST_MODE` (unset/false by default -- production behavior is
the default, never silently sandboxed).

## One call creates AND sends

`POST /v3/signature_request/send` -- exactly one of `files`/`file_urls` is
accepted, never both; this adapter always uses `files` (raw bytes), matching
`create_and_send()`'s contract of taking document bytes directly.

## The download-race gotcha

Calling the files endpoint immediately after the last signature reliably
returns 409 (surfaced here as `NotReadyError` by
`server/providers/esign.py`'s shared retry helper) -- the vendor is still
assembling the merged PDF. Callers should trigger `download_executed()` off
a `signature_request_all_signed` webhook or a `check_status()` poll reaching
`executed`, not immediately after sending the last reminder.
"""
from __future__ import annotations

import base64
import os

from server import config
from server.providers.esign import EnvelopeStatus, EsignDisabled, SignerSpec, _request_with_retry

_BASE = "https://api.hellosign.com/v3"
_TEST_MODE = os.environ.get("DROPBOXSIGN_TEST_MODE", "").lower() in ("1", "true", "yes")

_STATUS_MAP = {
    "awaiting_signature": "sent_for_signature",
    "signed": "executed",
    "declined": "void",
    "error": "void",
}


def _auth_header() -> dict[str, str]:
    api_key = config.get_secret("dropboxsign_api_key")
    if not api_key:
        raise EsignDisabled("Dropbox Sign is not configured: missing DROPBOXSIGN_API_KEY")
    token = base64.b64encode(f"{api_key}:".encode("ascii")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _multipart(fields: dict, files: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    """A small stdlib multipart/form-data encoder -- this codebase has no HTTP
    client dependency beyond urllib, and this is the only place across all
    four adapters that needs a raw file upload rather than a JSON body."""
    boundary = f"----crm-esign-{os.urandom(8).hex()}"
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            .encode("utf-8")
        )
    for field_name, filename, content, mime in files:
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; '
             f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode("utf-8")
            + content + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def create_and_send(
    *, subject: str, message: str, document_bytes: bytes, filename: str, mime: str,
    signers: list[SignerSpec],
) -> str:
    fields: dict = {"title": subject, "subject": subject, "message": message}
    for i, s in enumerate(sorted(signers, key=lambda s: s.order)):
        fields[f"signers[{i}][email_address]"] = s.email
        fields[f"signers[{i}][name]"] = s.name
        fields[f"signers[{i}][order]"] = str(s.order)
    if _TEST_MODE:
        fields["test_mode"] = "1"
    body, content_type = _multipart(fields, [("file[0]", filename, document_bytes, mime)])
    result = _request_with_retry(
        "POST", f"{_BASE}/signature_request/send",
        headers={**_auth_header(), "Content-Type": content_type}, data=body,
    )
    request_id = (result.get("signature_request") or {}).get("signature_request_id")
    if not request_id:
        raise EsignDisabled(f"Dropbox Sign send returned no signature_request_id: {result}")
    return request_id


def check_status(envelope_id: str) -> EnvelopeStatus:
    result = _request_with_retry(
        "GET", f"{_BASE}/signature_request/{envelope_id}", headers=_auth_header(),
    )
    request = result.get("signature_request") or {}
    signatures = request.get("signatures") or []
    if signatures and all(s.get("status_code") == "signed" for s in signatures):
        normalized = "executed"
    elif any(s.get("status_code") == "declined" for s in signatures):
        normalized = "void"
    elif any(s.get("status_code") == "signed" for s in signatures):
        normalized = "partially_signed"
    else:
        normalized = "sent_for_signature"
    raw = ",".join(s.get("status_code", "") for s in signatures)
    return EnvelopeStatus(status=normalized, raw_status=raw)


def download_executed(envelope_id: str) -> bytes:
    return _request_with_retry(
        "GET", f"{_BASE}/signature_request/files?signature_request_id={envelope_id}",
        headers=_auth_header(), raw=True,
    )

"""PandaDoc API adapter.

**Never live-tested.** Written from developers.pandadoc.com, not exercised
against a real account.

## Auth: API key, sandbox vs production is a DIFFERENT KEY, same host

Unlike Dropbox Sign's `test_mode` flag or DocuSign/Adobe's separate hosts,
PandaDoc's sandbox/production split is entirely which `PANDADOC_API_KEY` is
configured -- the endpoint (`api.pandadoc.com`) never changes. A sandbox key
caps requests at 10/min per endpoint, watermarks output "[DEV]", and
restricts recipients to your own email domain; a production key needs Sales
approval on an Enterprise plan. Which one is configured is entirely an
operator decision outside this code.

## Two calls, not one -- and a wait in between

`POST /documents` creates the document; it starts in status `uploaded` and
the vendor needs roughly 2-5 seconds to process it into `draft` before
`POST /documents/{id}/send` will accept it. Calling `/send` while still
`uploaded` is a documented failure mode. `_wait_for_draft()` polls
`check_status`-equivalent for exactly this transition before sending, so
`create_and_send()`'s contract (one call, from the caller's perspective)
holds despite the vendor needing two plus a wait.

## Completed-document download needs the OTHER endpoint

`GET /documents/{id}/download` works for draft/in-flight documents but
**401s on a sandbox key** for a genuinely completed one -- the completed path
is `/documents/{id}/download-protected`, and that one is production-key
only. `download_executed()` always calls the protected endpoint, which is
correct for a document this module considers "executed" but means this
adapter cannot demo a full round trip end-to-end on a sandbox key alone;
that is a PandaDoc account-tier fact, not a bug here.
"""
from __future__ import annotations

import json
import os
import time

from server import config
from server.providers.esign import (
    EnvelopeStatus,
    EsignDisabled,
    EsignError,
    SignerSpec,
    _request_with_retry,
)

_BASE = "https://api.pandadoc.com/public/v1"

_STATUS_MAP = {
    "document.draft": "draft",
    "document.uploaded": "draft",
    "document.sent": "sent_for_signature",
    "document.viewed": "sent_for_signature",
    "document.waiting_approval": "sent_for_signature",
    "document.approved": "sent_for_signature",
    "document.completed": "executed",
    "document.rejected": "void",
    "document.cancelled": "void",
    "document.declined": "void",
    "document.expired": "expired",
}


def _headers() -> dict[str, str]:
    api_key = config.get_secret("pandadoc_api_key")
    if not api_key:
        raise EsignDisabled("PandaDoc is not configured: missing PANDADOC_API_KEY")
    return {"Authorization": f"API-Key {api_key}"}


def _multipart(fields: dict, filename: str, content: bytes, mime: str) -> tuple[bytes, str]:
    boundary = f"----crm-esign-{os.urandom(8).hex()}"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="data"\r\n\r\n'
        f'{json.dumps(fields)}\r\n'.encode("utf-8"),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
         f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode("utf-8")
        + content + b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _wait_for_draft(document_id: str, *, timeout: int = 30) -> None:
    """Poll until the document leaves 'uploaded' for 'draft'. A document
    still 'uploaded' when /send is called is a documented PandaDoc failure
    mode -- this is what keeps create_and_send() a single logical operation
    for the caller despite the vendor's two-call, wait-in-between reality."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _request_with_retry(
            "GET", f"{_BASE}/documents/{document_id}", headers=_headers(),
        )
        status = result.get("status", "")
        if status == "document.draft":
            return
        if status not in ("document.uploaded",):
            raise EsignError(f"PandaDoc document {document_id} left uploaded/draft: {status!r}")
        time.sleep(1)
    raise EsignError(f"PandaDoc document {document_id} did not reach draft within {timeout}s")


def create_and_send(
    *, subject: str, message: str, document_bytes: bytes, filename: str, mime: str,
    signers: list[SignerSpec],
) -> str:
    fields = {
        "name": subject,
        "recipients": [
            {"email": s.email, "first_name": s.name.split(" ", 1)[0],
             "last_name": s.name.split(" ", 1)[-1] if " " in s.name else "",
             "role": s.role}
            for s in sorted(signers, key=lambda s: s.order)
        ],
    }
    body, content_type = _multipart(fields, filename, document_bytes, mime)
    result = _request_with_retry(
        "POST", f"{_BASE}/documents",
        headers={**_headers(), "Content-Type": content_type}, data=body,
    )
    document_id = result.get("id")
    if not document_id:
        raise EsignDisabled(f"PandaDoc document creation returned no id: {result}")

    _wait_for_draft(document_id)

    _request_with_retry(
        "POST", f"{_BASE}/documents/{document_id}/send",
        headers={**_headers(), "Content-Type": "application/json"},
        data=json.dumps({"message": message}).encode(),
    )
    return document_id


def check_status(envelope_id: str) -> EnvelopeStatus:
    result = _request_with_retry(
        "GET", f"{_BASE}/documents/{envelope_id}", headers=_headers(),
    )
    raw = result.get("status", "")
    return EnvelopeStatus(status=_STATUS_MAP.get(raw, "sent_for_signature"), raw_status=raw)


def download_executed(envelope_id: str) -> bytes:
    # NOT /download -- that 401s on a completed document under a sandbox key.
    # This endpoint is documented as production-key-only for a real signed doc.
    return _request_with_retry(
        "GET", f"{_BASE}/documents/{envelope_id}/download-protected",
        headers=_headers(), raw=True,
    )

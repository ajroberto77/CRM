"""Adobe Acrobat Sign (formerly Adobe Sign) API v6 adapter.

**Never live-tested.** Written from developer.adobe.com/acrobat-sign and
helpx.adobe.com/sign, not exercised against a real account.

## Auth: a static Integration Key, not OAuth

Adobe's docs describe OAuth2 authorization-code as the modern path, but also
document Integration Keys -- a static bearer token generated directly in the
Acrobat Sign account UI -- as valid for server-to-server use with no per-user
consent dance. That is the model here, for the same reason Dropbox Sign's API
key is: a CRM background job has no user present to run an OAuth redirect
through. `Authorization: Bearer <integration_key>` on every call.

**Scope gotcha, worth getting right the first time:** the scope needed to
create and send an agreement is `agreement_write`. `agreement_send` is
deprecated as of API v6 and no longer accepted -- any sample code copied from
an older v5-era guide almost certainly uses the wrong name.

## Base URL is shard-discovered, not fixed

`GET https://api.adobesign.com/api/rest/v6/baseUris` (with the bearer token
already attached) returns `apiAccessPoint` -- the account's real regional
host (na1/na2/eu1/jp1/...). Every other call goes to
`{apiAccessPoint}/api/rest/v6/...`. Cached per process for the same reason
DocuSign's base_uri is: hardcoding `api.adobesign.com` for every call works
for the discovery endpoint alone and silently fails for any account not on
that specific shard.

## Two calls: upload, then create-and-send

Documents are uploaded first as **transient documents**
(`POST /transientDocuments`, multipart), which returns a short-lived
`transientDocumentId`. The agreement is then created referencing that id,
with `"state": "IN_PROCESS"` to send immediately (vs `"AUTHORING"` for a
draft). The transient document id is not meant to be cached or reused across
a long delay -- `create_and_send()` uploads and creates back to back, never
holding a transient id across calls.

## Declined-state representation is the softest-confirmed part of this API

Adobe's own guidance is to verify the exact declined/cancelled representation
against a live sandbox agreement rather than trust older docs. `_STATUS_MAP`
below maps `CANCELLED`/`ABORTED` to this module's `void`, which is the
best-documented interpretation found during research -- confirm this against
a real declined agreement before depending on it.
"""
from __future__ import annotations

import json
import os

from server import config
from server.providers.esign import EnvelopeStatus, EsignDisabled, SignerSpec, _request_with_retry

_DISCOVERY_HOST = os.environ.get("ADOBESIGN_DISCOVERY_HOST", "api.adobesign.com")

_STATUS_MAP = {
    "AUTHORING": "draft",
    "OUT_FOR_SIGNATURE": "sent_for_signature",
    "OUT_FOR_APPROVAL": "sent_for_signature",
    "OUT_FOR_ACCEPTANCE": "sent_for_signature",
    "OUT_FOR_DELIVERY": "sent_for_signature",
    "OUT_FOR_FORM_FILLING": "sent_for_signature",
    "DELIVERED": "sent_for_signature",
    "FORM_FILLED": "sent_for_signature",
    "SIGNED": "executed",
    "APPROVED": "executed",
    "ACCEPTED": "executed",
    "EXPIRED": "expired",
    "CANCELLED": "void",
    "ABORTED": "void",
}

_base_cache: dict[str, str] = {}  # integration_key -> apiAccessPoint


def _bearer() -> str:
    key = config.get_secret("adobesign_integration_key")
    if not key:
        raise EsignDisabled("Adobe Sign is not configured: missing ADOBESIGN_INTEGRATION_KEY")
    return key


def _resolve_base(token: str) -> str:
    cached = _base_cache.get(token)
    if cached:
        return cached
    result = _request_with_retry(
        "GET", f"https://{_DISCOVERY_HOST}/api/rest/v6/baseUris",
        headers={"Authorization": f"Bearer {token}"},
    )
    access_point = result.get("apiAccessPoint")
    if not access_point:
        raise EsignDisabled(f"Adobe Sign baseUris returned no apiAccessPoint: {result}")
    base = f"{access_point.rstrip('/')}/api/rest/v6"
    _base_cache[token] = base
    return base


def _authenticated_base() -> tuple[str, dict[str, str]]:
    token = _bearer()
    base = _resolve_base(token)
    return base, {"Authorization": f"Bearer {token}"}


def _multipart(fields: dict, filename: str, content: bytes, mime: str) -> tuple[bytes, str]:
    boundary = f"----crm-esign-{os.urandom(8).hex()}"
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            .encode("utf-8")
        )
    parts.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="File"; '
         f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode("utf-8")
        + content + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def create_and_send(
    *, subject: str, message: str, document_bytes: bytes, filename: str, mime: str,
    signers: list[SignerSpec],
) -> str:
    base, headers = _authenticated_base()

    upload_body, content_type = _multipart(
        {"File-Name": filename, "Mime-Type": mime}, filename, document_bytes, mime,
    )
    upload_result = _request_with_retry(
        "POST", f"{base}/transientDocuments",
        headers={**headers, "Content-Type": content_type}, data=upload_body,
    )
    transient_id = upload_result.get("transientDocumentId")
    if not transient_id:
        raise EsignDisabled(f"Adobe Sign transient upload returned no id: {upload_result}")

    body = {
        "fileInfos": [{"transientDocumentId": transient_id}],
        "name": subject,
        "participantSetsInfo": [
            {
                "memberInfos": [{"email": s.email}],
                "order": s.order,
                "role": "SIGNER",
            }
            for s in sorted(signers, key=lambda s: s.order)
        ],
        "signatureType": "ESIGN",
        "state": "IN_PROCESS",  # send immediately, vs AUTHORING for a draft
        "message": message,
    }
    result = _request_with_retry(
        "POST", f"{base}/agreements",
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps(body).encode(),
    )
    agreement_id = result.get("id")
    if not agreement_id:
        raise EsignDisabled(f"Adobe Sign agreement creation returned no id: {result}")
    return agreement_id


def check_status(envelope_id: str) -> EnvelopeStatus:
    base, headers = _authenticated_base()
    result = _request_with_retry("GET", f"{base}/agreements/{envelope_id}", headers=headers)
    raw = result.get("status", "")
    return EnvelopeStatus(status=_STATUS_MAP.get(raw, "sent_for_signature"), raw_status=raw)


def download_executed(envelope_id: str) -> bytes:
    base, headers = _authenticated_base()
    return _request_with_retry(
        "GET",
        f"{base}/agreements/{envelope_id}/combinedDocument"
        f"?attachAuditReport=true&attachSupportingDocuments=true",
        headers=headers, raw=True,
    )

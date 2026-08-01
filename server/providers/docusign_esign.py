"""DocuSign eSignature API v2.1 adapter.

**Never live-tested.** Written from DocuSign's current REST documentation
(developers.docusign.com), not exercised against a real account. Verify
against a demo-environment sandbox before enabling this provider unattended.

## Auth: OAuth2 JWT Bearer Grant

Server-to-server, no user present -- the right model for a CRM background job.
Requires an RSA keypair generated in DocuSign Admin under the Integration
Key's Service Integration settings, and a one-time consent grant for the
impersonated user (`docusign_user_id`).

Token exchange: `POST {auth_host}/oauth/token` with a JWT assertion, RS256-
signed with the private key. The signing itself uses `cryptography` (see
`server/providers/esign.py`'s dependency note) rather than any hand-rolled
RSA/PKCS1v15 code -- that combination is exactly where a stdlib-only
implementation would become a real security bug.

## Base URI is not fixed

Demo (`account-d.docusign.com`) and production (`account.docusign.com`) are
separate app registrations with separate keys. After auth, `GET
/oauth/userinfo` on the same host returns `accounts[].base_uri` + `account_id`
-- production accounts are sharded across data centers (na2, na3, eu, ...) and
this is the only way to learn which shard an account is on. Cached for the
process lifetime; a fresh token does not mean a fresh lookup.

## One call creates AND sends

`POST {base_uri}/restapi/v2.1/accounts/{accountId}/envelopes` with
`"status": "sent"` both creates the envelope and sends it -- unlike PandaDoc
and Adobe Sign, which need two calls.

## Gotchas from research, worth restating in code

- Build against v2.1 only; v2.0 is maintenance-mode.
- 3,000 calls/hour, ~500/30s burst, both now return 429 (older docs described
  400 for the burst case -- do not trust that if you find it in an old guide).
- A PEM private key with its line breaks collapsed into one line is the
  single most common cause of `no_valid_keys_or_signatures` -- `_load_pem` is
  intentionally strict about this rather than trying to repair it.
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from server import config
from server.providers.esign import EnvelopeStatus, EsignDisabled, SignerSpec, _request_with_retry

_AUTH_HOST = os.environ.get("DOCUSIGN_AUTH_HOST", "account-d.docusign.com")
_SCOPES = "signature impersonation"

# GRAPH-style status mapping -- DocuSign's own vocabulary collapsed into
# server/providers/esign.py's closed STATUSES set.
_STATUS_MAP = {
    "created": "draft",
    "sent": "sent_for_signature",
    "delivered": "sent_for_signature",
    "completed": "executed",
    "declined": "void",
    "voided": "void",
}

_base_uri_cache: dict[str, tuple[str, str]] = {}  # integration_key -> (base_uri, account_id)


def _require_config() -> dict[str, str]:
    values = {
        "integration_key": config.get_secret("docusign_integration_key"),
        "user_id": config.get_secret("docusign_user_id"),
        "private_key": config.get_secret("docusign_private_key"),
    }
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise EsignDisabled(f"DocuSign is not configured: missing {', '.join(missing)}")
    return values


def _load_pem(private_key_pem: str):
    try:
        return serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    except ValueError as exc:
        raise EsignDisabled(
            "DOCUSIGN_PRIVATE_KEY is not a valid PEM private key -- check that line "
            "breaks were preserved when the key was stored (a single-line PEM is "
            "the most common cause of this)"
        ) from exc


def _sign_jwt(integration_key: str, user_id: str, private_key_pem: str) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iss": integration_key,
        "sub": user_id,
        "aud": _AUTH_HOST,
        "iat": now,
        "exp": now + 3600,
        "scope": _SCOPES,
    }
    segments = [
        base64.urlsafe_b64encode(json.dumps(part, separators=(",", ":")).encode()).rstrip(b"=")
        for part in (header, payload)
    ]
    signing_input = b".".join(segments)
    key = _load_pem(private_key_pem)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return (signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode("ascii")


def _get_access_token() -> str:
    creds = _require_config()
    assertion = _sign_jwt(creds["integration_key"], creds["user_id"], creds["private_key"])
    body = (
        f"grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion={assertion}"
    ).encode()
    result = _request_with_retry(
        "POST", f"https://{_AUTH_HOST}/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"}, data=body,
    )
    token = result.get("access_token")
    if not token:
        raise EsignDisabled(f"DocuSign token exchange returned no access_token: {result}")
    return token


def _resolve_account(access_token: str, integration_key: str) -> tuple[str, str]:
    cached = _base_uri_cache.get(integration_key)
    if cached:
        return cached
    result = _request_with_retry(
        "GET", f"https://{_AUTH_HOST}/oauth/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    accounts = result.get("accounts") or []
    if not accounts:
        raise EsignDisabled("DocuSign userinfo returned no accounts for this integration key")
    account = next((a for a in accounts if a.get("is_default")), accounts[0])
    resolved = (account["base_uri"], account["account_id"])
    _base_uri_cache[integration_key] = resolved
    return resolved


def _authenticated_base() -> tuple[str, dict[str, str]]:
    creds = _require_config()
    token = _get_access_token()
    base_uri, account_id = _resolve_account(token, creds["integration_key"])
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    return f"{base_uri}/restapi/v2.1/accounts/{account_id}", headers


def create_and_send(
    *, subject: str, message: str, document_bytes: bytes, filename: str, mime: str,
    signers: list[SignerSpec],
) -> str:
    base, headers = _authenticated_base()
    body = {
        "emailSubject": subject,
        "emailBlurb": message,
        "status": "sent",  # one call creates AND sends
        "documents": [{
            "documentBase64": base64.b64encode(document_bytes).decode("ascii"),
            "name": filename,
            "fileExtension": (filename.rsplit(".", 1)[-1] if "." in filename else "pdf"),
            "documentId": "1",
        }],
        "recipients": {
            "signers": [
                {
                    "email": s.email, "name": s.name,
                    "recipientId": str(i + 1), "routingOrder": str(s.order),
                }
                for i, s in enumerate(signers)
            ]
        },
    }
    result = _request_with_retry(
        "POST", f"{base}/envelopes", headers=headers, data=json.dumps(body).encode(),
    )
    envelope_id = result.get("envelopeId")
    if not envelope_id:
        raise EsignDisabled(f"DocuSign envelope creation returned no envelopeId: {result}")
    return envelope_id


def check_status(envelope_id: str) -> EnvelopeStatus:
    base, headers = _authenticated_base()
    result = _request_with_retry("GET", f"{base}/envelopes/{envelope_id}", headers=headers)
    raw = result.get("status", "")
    return EnvelopeStatus(status=_STATUS_MAP.get(raw, "sent_for_signature"), raw_status=raw)


def download_executed(envelope_id: str) -> bytes:
    base, headers = _authenticated_base()
    headers = {**headers, "Content-Type": "application/pdf"}
    return _request_with_retry(
        "GET", f"{base}/envelopes/{envelope_id}/documents/combined",
        headers=headers, raw=True,
    )

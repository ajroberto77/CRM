"""Microsoft Graph OAuth token client and generic request wrapper.

Public-client PKCE against the `/common` tenant (works for personal AND
work/school Microsoft accounts) -- ported from Cal's
`microsoft_graph_client.py`, confirmed live there. `config.py`'s
`microsoft_oauth_client_secret` slot is sent alongside the PKCE exchange
when set (harmless for a public-client app registration, required for a
confidential one) -- this codebase's registration shape isn't assumed
either way.

Shared by `microsoft_oauth.py` (the link flow), `microsoft_contacts.py`
(M4), and `microsoft_calendar.py` (M5) -- one Graph client, not one per
axis (R1).

## Retry

429/503/504 honoring `Retry-After` via `server/net/http_retry.py` (R1) --
no bespoke retry loop here.

## Token storage

Access tokens are minted per call, never persisted. The refresh token
lives in `server/core/accounts.py`'s `account_credentials.payload`; if
Microsoft rotates it on refresh, the rotated value is saved back
immediately so the old one is never reused.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, Optional

from server import config
from server.core import accounts
from server.net import http_retry

TENANT = "common"
AUTH_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphError(RuntimeError):
    pass


class GraphDisabled(GraphError):
    """No client id configured, or no credentials stored for this account."""


def client_id() -> str:
    value = config.get_secret("microsoft_oauth_client_id")
    if not value:
        raise GraphDisabled("MICROSOFT_OAUTH_CLIENT_ID is not set")
    return value


def _token_request(payload: dict[str, str]) -> dict[str, Any]:
    client_secret = config.get_secret("microsoft_oauth_client_secret")
    body = dict(payload)
    if client_secret:
        body["client_secret"] = client_secret
    data = urllib.parse.urlencode(body).encode("utf-8")
    try:
        return http_retry.request_with_retry(
            "POST", TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=data, max_retries=3,
        )
    except http_retry.HTTPRetryError as exc:
        raise GraphError(f"token request failed: {exc}") from exc


def exchange_code(
    *, code: str, redirect_uri: str, code_verifier: str, scopes: str
) -> dict[str, Any]:
    return _token_request({
        "client_id": client_id(),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "scope": scopes,
    })


def mint_access_token(org_id: str, account_id: str) -> str:
    """Refresh-token grant, minted fresh every call -- no in-memory cache.
    A revoked/expired refresh token surfaces as GraphError; the caller
    decides how to record that (never a permanent latch -- safety rule 5)."""
    creds = accounts.get_credentials(org_id, account_id)
    if creds is None or creds.get("credential_type") != "oauth":
        raise GraphDisabled(f"no stored oauth credentials for account {account_id}")
    payload = creds["payload"]
    data = _token_request({
        "client_id": client_id(),
        "grant_type": "refresh_token",
        "refresh_token": payload["refresh_token"],
        "scope": payload["scopes"],
    })
    access_token = data.get("access_token")
    if not access_token:
        raise GraphError(f"token refresh response had no access_token: {data}")
    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != payload["refresh_token"]:
        accounts.save_credentials(
            org_id, account_id, "oauth", {**payload, "refresh_token": new_refresh}
        )
    return access_token


def _resolve_url(path_or_url: str) -> str:
    # A bare Graph-relative path, or a full URL (e.g. an @odata.nextLink from
    # a delta query) -- callers pass either.
    return path_or_url if path_or_url.startswith("http") else f"{GRAPH_BASE}{path_or_url}"


def request_with_token(
    method: str, path_or_url: str, access_token: str, *,
    body: Optional[dict[str, Any]] = None, extra_headers: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """For the one bootstrap call (`GET /me`) made before any account
    credentials row exists yet -- everything else goes through request()."""
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        return http_retry.request_with_retry(
            method, _resolve_url(path_or_url), headers=headers, data=data, max_retries=5,
        )
    except http_retry.HTTPRetryError as exc:
        raise GraphError(f"{exc} (url={path_or_url})") from exc


def request(
    method: str, path_or_url: str, org_id: str, account_id: str, *,
    body: Optional[dict[str, Any]] = None, extra_headers: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    access_token = mint_access_token(org_id, account_id)
    return request_with_token(
        method, path_or_url, access_token, body=body, extra_headers=extra_headers
    )


def request_bytes(method: str, path_or_url: str, org_id: str, account_id: str) -> bytes:
    access_token = mint_access_token(org_id, account_id)
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        return http_retry.request_with_retry(
            method, _resolve_url(path_or_url), headers=headers, max_retries=5, raw=True,
        )
    except http_retry.HTTPRetryError as exc:
        raise GraphError(f"{exc} (url={path_or_url})") from exc

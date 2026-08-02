"""Google OAuth token client and generic request wrapper.

Confidential-client flow (Google's "Web application" client type requires
a client secret, unlike Microsoft's public-client PKCE option) with PKCE
added on top as defense-in-depth. Structural mirror of
`microsoft_graph.py` -- same shape, Google's own endpoints -- shared by
`google_oauth.py` (the link flow), `google_contacts.py` (M4), and
`google_calendar.py` (M5).

## Retry

429/503/504 honoring `Retry-After` via `server/net/http_retry.py` (R1).

## Token storage

Same discipline as Microsoft: access tokens are minted per call, never
persisted. Google does not rotate the refresh token on every refresh call
(unlike Microsoft) -- a rotated value is saved back when Google does
return one, otherwise the stored refresh token is left untouched, which is
the normal case, not a failure.

Unlike Graph (one base URL for every resource), Google's APIs span
separate hosts per product (People API, Calendar API) -- callers always
pass a full URL, there is no relative-path resolution here.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, Optional

from server import config
from server.core import accounts
from server.net import http_retry

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


class GoogleApiError(RuntimeError):
    pass


class GoogleApiDisabled(GoogleApiError):
    """No client id/secret configured, or no credentials stored for this account."""


def client_id() -> str:
    value = config.get_secret("google_oauth_client_id")
    if not value:
        raise GoogleApiDisabled("GOOGLE_OAUTH_CLIENT_ID is not set")
    return value


def client_secret() -> str:
    value = config.get_secret("google_oauth_client_secret")
    if not value:
        raise GoogleApiDisabled("GOOGLE_OAUTH_CLIENT_SECRET is not set")
    return value


def _token_request(payload: dict[str, str]) -> dict[str, Any]:
    body = {**payload, "client_secret": client_secret()}
    data = urllib.parse.urlencode(body).encode("utf-8")
    try:
        return http_retry.request_with_retry(
            "POST", TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=data, max_retries=3,
        )
    except http_retry.HTTPRetryError as exc:
        raise GoogleApiError(f"token request failed: {exc}") from exc


def exchange_code(*, code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
    return _token_request({
        "client_id": client_id(),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    })


def mint_access_token(org_id: str, account_id: str) -> str:
    creds = accounts.get_credentials(org_id, account_id)
    if creds is None or creds.get("credential_type") != "oauth":
        raise GoogleApiDisabled(f"no stored oauth credentials for account {account_id}")
    payload = creds["payload"]
    data = _token_request({
        "client_id": client_id(),
        "grant_type": "refresh_token",
        "refresh_token": payload["refresh_token"],
    })
    access_token = data.get("access_token")
    if not access_token:
        raise GoogleApiError(f"token refresh response had no access_token: {data}")
    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != payload["refresh_token"]:
        accounts.save_credentials(
            org_id, account_id, "oauth", {**payload, "refresh_token": new_refresh}
        )
    return access_token


def request_with_token(
    method: str, url: str, access_token: str, *,
    body: Optional[dict[str, Any]] = None, extra_headers: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """For the one bootstrap call (userinfo) made before any account
    credentials row exists yet -- everything else goes through request()."""
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        return http_retry.request_with_retry(
            method, url, headers=headers, data=data, max_retries=5
        )
    except http_retry.HTTPRetryError as exc:
        raise GoogleApiError(f"{exc} (url={url})") from exc


def request(
    method: str, url: str, org_id: str, account_id: str, *,
    body: Optional[dict[str, Any]] = None, extra_headers: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    access_token = mint_access_token(org_id, account_id)
    return request_with_token(method, url, access_token, body=body, extra_headers=extra_headers)

"""Google's half of the account-link flow (`server/core/account_link.py`'s
other `elif`). Confidential-client OAuth2 with PKCE layered on top,
structurally mirroring `microsoft_oauth.py` -- same shape, Google's own
endpoints/scopes.

Scopes cover contacts (M4, read-only -- this milestone only syncs in, it
never writes back) AND calendar (M5, read/write, needed to create/update
events) up front, same reasoning as the Microsoft side: one consent
screen, not two.

`access_type=offline` + `prompt=consent` are both required to reliably get
a refresh token back -- Google only issues one on first consent by
default, and a reconnect after a revoke needs `prompt=consent` to force a
fresh one rather than silently succeeding with no refresh_token at all.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from server.core import accounts
from server.core.account_link import LinkError, LinkUpstreamError, pkce_pair
from server.providers import google_api as ga

SCOPES = " ".join([
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/calendar",
])


def start_link(org_id: str, account_id: str, redirect_uri: str) -> str:
    verifier, challenge = pkce_pair()
    state = accounts.create_oauth_pending(org_id, account_id, "google", verifier)
    try:
        client_id = ga.client_id()
    except ga.GoogleApiError as exc:
        raise LinkUpstreamError(str(exc)) from exc
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{ga.AUTH_URL}?{urllib.parse.urlencode(params)}"


def complete_link(org_id: str, state: str, code: str, redirect_uri: str) -> dict[str, Any]:
    pending = accounts.pop_oauth_pending(org_id, state)
    if pending is None:
        raise LinkError("unknown or expired oauth state")
    account_id = str(pending["account_id"])

    try:
        tokens = ga.exchange_code(
            code=code, redirect_uri=redirect_uri, code_verifier=pending["code_verifier"]
        )
    except ga.GoogleApiError as exc:
        accounts.update_account_status(org_id, account_id, "error", f"token exchange failed: {exc}")
        raise LinkUpstreamError(f"token exchange failed: {exc}") from exc

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not access_token or not refresh_token:
        accounts.update_account_status(
            org_id, account_id, "error",
            "token exchange response missing tokens (reconnect may be required -- "
            "Google only returns a refresh token on first consent)",
        )
        raise LinkError("token exchange response missing tokens")

    accounts.save_credentials(
        org_id, account_id, "oauth", {"refresh_token": refresh_token, "scopes": SCOPES}
    )

    try:
        me = ga.request_with_token("GET", ga.USERINFO_URL, access_token)
    except ga.GoogleApiError as exc:
        accounts.update_account_status(org_id, account_id, "error", f"post-link userinfo failed: {exc}")
        raise LinkUpstreamError(f"post-link userinfo failed: {exc}") from exc

    email = me.get("email") or ""
    accounts.activate_account(org_id, account_id, email)
    return accounts.get_account(org_id, account_id)

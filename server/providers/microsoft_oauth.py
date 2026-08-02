"""Microsoft's half of the account-link flow (`server/core/account_link.py`'s
one `elif` for this provider). PKCE public-client flow against
`microsoft_graph.py`'s `/common`-tenant endpoints, ported from Cal's
`microsoft_oauth.py`.

Scopes cover contacts (M4) AND calendar (M5) up front -- one consent
screen, not two, since both axes reuse the same connected account and a
second OAuth round-trip later would need re-consent anyway.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from server.core import accounts
from server.core.account_link import LinkError, LinkUpstreamError, pkce_pair
from server.providers import microsoft_graph as gc

SCOPES = "offline_access User.Read Contacts.ReadWrite Calendars.ReadWrite"


def start_link(org_id: str, account_id: str, redirect_uri: str) -> str:
    verifier, challenge = pkce_pair()
    state = accounts.create_oauth_pending(org_id, account_id, "microsoft", verifier)
    try:
        client_id = gc.client_id()
    except gc.GraphError as exc:
        raise LinkUpstreamError(str(exc)) from exc
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{gc.AUTH_URL}?{urllib.parse.urlencode(params)}"


def complete_link(org_id: str, state: str, code: str, redirect_uri: str) -> dict[str, Any]:
    pending = accounts.pop_oauth_pending(org_id, state)
    if pending is None:
        raise LinkError("unknown or expired oauth state")
    account_id = str(pending["account_id"])

    try:
        tokens = gc.exchange_code(
            code=code, redirect_uri=redirect_uri,
            code_verifier=pending["code_verifier"], scopes=SCOPES,
        )
    except gc.GraphError as exc:
        accounts.update_account_status(org_id, account_id, "error", f"token exchange failed: {exc}")
        raise LinkUpstreamError(f"token exchange failed: {exc}") from exc

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not access_token or not refresh_token:
        accounts.update_account_status(
            org_id, account_id, "error", "token exchange response missing tokens"
        )
        raise LinkError("token exchange response missing tokens")

    accounts.save_credentials(
        org_id, account_id, "oauth", {"refresh_token": refresh_token, "scopes": SCOPES}
    )

    try:
        me = gc.request_with_token("GET", "/me", access_token)
    except gc.GraphError as exc:
        accounts.update_account_status(org_id, account_id, "error", f"post-link /me failed: {exc}")
        raise LinkUpstreamError(f"post-link /me failed: {exc}") from exc

    email = me.get("mail") or me.get("userPrincipalName") or ""
    accounts.activate_account(org_id, account_id, email)
    return accounts.get_account(org_id, account_id)

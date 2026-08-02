"""Connected Microsoft/Google accounts (M4) over HTTP.

Not routed through `records.py`'s generic entity CRUD -- same reasoning as
`server/api/settings.py`: `server/core/accounts.py` deliberately keeps
credentials out of the generic repository's field-mask machinery.

Every route is scoped to the CALLING user's own accounts, not admin-gated
-- linking a mailbox/calendar is a personal action (whose account it is
matters, not whether the caller is an admin).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from server import config
from server.api.auth import current_principal
from server.core import account_link, accounts
from server.core.permissions import Principal

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _redirect_uri() -> str:
    return f"{config.get_public_base_url()}/accounts/oauth/callback"


def _public(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(account["id"]),
        "provider": account["provider"],
        "email_address": account["email_address"],
        "status": account["status"],
        "status_detail": account["status_detail"],
        "created_at": str(account["created_at"]),
        "updated_at": str(account["updated_at"]),
    }


@router.get("")
def list_accounts(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    rows = accounts.list_accounts(principal.org_id, user_id=principal.user_id)
    return {"accounts": [_public(a) for a in rows]}


@router.post("/{provider}/link")
def start_link(
    provider: str, principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    if provider not in accounts.PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown provider {provider!r} (known: {', '.join(accounts.PROVIDERS)})",
        )
    account = accounts.create_pending_account(principal.org_id, principal.user_id, provider)
    url = account_link.start_link(principal.org_id, str(account["id"]), provider, _redirect_uri())
    return {"authorize_url": url}


@router.get("/oauth/callback")
def oauth_callback(
    state: str = Query(...), code: str = Query(...),
    principal: Principal = Depends(current_principal),
) -> RedirectResponse:
    account = account_link.complete_link(principal.org_id, state, code, _redirect_uri())
    if str(account["user_id"]) != principal.user_id:
        # The pending row belonged to a different user in the same org --
        # never happens under normal use (state is an unguessable random
        # token only the linking user's own browser ever sees), but the
        # check costs nothing and closes the gap outright rather than
        # trusting the token alone.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this link does not belong to you")
    return RedirectResponse(
        url="/admin/connected-accounts?linked=1", status_code=status.HTTP_302_FOUND
    )


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def disconnect(account_id: str, principal: Principal = Depends(current_principal)) -> None:
    account = accounts.get_account(principal.org_id, account_id)
    if account is None or str(account["user_id"]) != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such connected account")
    accounts.delete_account(principal.org_id, account_id)

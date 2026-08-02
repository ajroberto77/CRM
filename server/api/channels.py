"""Connected Signal/Telegram identities (M6) over HTTP -- links a channel
identity (a phone number or Telegram handle) to the CALLING user's own
account, so an inbound command from that identity resolves back to them
(`server/channels/dispatch.py`).

Not routed through `records.py`'s generic entity CRUD -- same reasoning
as `server/api/accounts.py`: this is identity/auth-adjacent, and every
route is scoped to the calling principal, never admin-gated and never
accepting a target user_id from the request body.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from server.api.auth import current_principal
from server.core import identity, user_channels
from server.core.permissions import Principal

router = APIRouter(prefix="/channels", tags=["channels"])

# The messaging axis (server/channels/dispatch.py) only knows signal and
# telegram -- email/phone/handle stay in identity.CHANNEL_KINDS for
# contact_channels' broader use, but linking one of those here would
# create a user_channels row no dispatcher ever reads.
_LINKABLE_KINDS = ("signal", "telegram")


class LinkRequest(BaseModel):
    kind: str
    value: str


def _public(channel: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(channel["id"]),
        "kind": channel["kind"],
        "value_raw": channel["value_raw"],
        "created_at": str(channel["created_at"]),
    }


@router.get("")
def list_channels(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    rows = user_channels.list_channels(principal.org_id, user_id=principal.user_id)
    return {"channels": [_public(c) for c in rows]}


@router.post("/link")
def link_channel(
    body: LinkRequest, principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    if body.kind not in _LINKABLE_KINDS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown channel kind {body.kind!r} (known: {', '.join(_LINKABLE_KINDS)})",
        )
    try:
        channel = user_channels.link_channel(
            principal.org_id, principal.user_id, body.kind, body.value
        )
    except identity.NormalizationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except user_channels.AlreadyLinkedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _public(channel)


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def unlink_channel(channel_id: str, principal: Principal = Depends(current_principal)) -> None:
    channel = user_channels.get_channel(principal.org_id, channel_id)
    if channel is None or str(channel["user_id"]) != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such connected channel")
    user_channels.unlink_channel(principal.org_id, channel_id)

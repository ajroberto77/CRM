"""Approve/decline actions over `server/core/proposals.py`'s queue.

Listing and detail already come free from the generic `/records/
proposed_change` route (R4) -- this router adds only the two real state
transitions, which must go through `proposals.py`'s compare-and-swap
functions rather than a generic PATCH: every field on `proposed_change` is
`writable=False` through the generic REST path for exactly this reason
(safety rule 3's single execution choke point).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from server.api.auth import current_principal
from server.core import proposals
from server.core.permissions import Principal

router = APIRouter(prefix="/proposals", tags=["proposals"])


@router.post("/{proposal_id}/approve")
def approve(proposal_id: str, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    principal.require("edit", "proposed_change")
    try:
        return proposals.approve(
            principal.org_id, proposal_id, decided_by=f"user:{principal.user_id}"
        )
    except proposals.AlreadyDecidedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/{proposal_id}/decline")
def decline(proposal_id: str, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    principal.require("edit", "proposed_change")
    try:
        return proposals.decline(
            principal.org_id, proposal_id, decided_by=f"user:{principal.user_id}"
        )
    except proposals.AlreadyDecidedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

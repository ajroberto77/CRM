"""Per-org product settings (`server/core/settings.py`) over HTTP.

Not routed through `records.py`'s generic entity CRUD (R4 doesn't apply
here — settings are a per-org singleton keyed by section, not a list of
records a table/detail view would browse, and the read-merge-write
discipline safety rule 2 requires doesn't fit the generic repository's
per-column merge semantics). This is the one small, dedicated router that
covers it, plus the LLM axis's status/test-connection endpoints, which
belong here rather than in `server/llm/router.py` itself (that module has
no HTTP concerns of its own).

Every route is admin-only: settings configure how the whole org behaves
(which LLM provider, what secrets are implied), not a per-user preference.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from server.api.auth import require_admin_principal
from server.core import settings as core_settings
from server.core.permissions import Principal
from server.llm import router as llm_router

router = APIRouter(prefix="/settings", tags=["settings"])


class UpdateSettingsBody(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)
    clear: list[str] = Field(default_factory=list)


class TestConnectionBody(BaseModel):
    overrides: dict[str, Any] = Field(default_factory=dict)


@router.get("/{section}")
def get_settings(
    section: str, principal: Principal = Depends(require_admin_principal)
) -> dict[str, Any]:
    return {"section": section, "values": core_settings.get_settings(principal.org_id, section)}


@router.patch("/{section}")
def update_settings(
    section: str, body: UpdateSettingsBody,
    principal: Principal = Depends(require_admin_principal),
) -> dict[str, Any]:
    values = core_settings.update_settings(
        principal.org_id, section, body.changes,
        clear=body.clear, updated_by=principal.user_id,
    )
    return {"section": section, "values": values}


@router.get("/llm/status")
def llm_status(principal: Principal = Depends(require_admin_principal)) -> dict[str, Any]:
    """Reachability of every provider adapter, using settings as currently
    saved -- what the settings page's status dots render from."""
    return {"providers": llm_router.check_llm_status(principal.org_id)}


@router.post("/llm/test")
def llm_test_connection(
    body: TestConnectionBody, principal: Principal = Depends(require_admin_principal),
) -> dict[str, Any]:
    """Fires one real call with unsaved form values -- never touches
    core.settings, so a user can try a provider/model/key combination before
    committing to Save."""
    return llm_router.test_llm_connection(principal.org_id, body.overrides)

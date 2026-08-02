"""The FastAPI application.

M0 surface only: health, first-run setup, login/logout, and the authenticated
identity endpoint. Entity CRUD is deliberately absent -- it arrives in M1 as a
generic router driven by the entity registry, not as hand-written routes (R4).

Startup runs the migration and then verifies RLS is actually armed, refusing to
serve if it is not. A schema whose policies are enabled but not FORCEd looks
completely healthy in every functional test while the app role sees every
tenant's rows, so it is checked rather than assumed.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from server import config
from server.api import accounts as accounts_api
from server.api import auth, records
from server.api import channels as channels_api
from server.api import proposals as proposals_api
from server.api import search as search_api
from server.api import settings as settings_api
from server.core import account_link, associations, modules, passwords, permissions, query, registry, repository, sessions, users
from server.core import settings as core_settings
from server.core.permissions import Principal
from server.db import pool, schema
from server.llm import embeddings as embeddings_llm
from server.llm import router as llm_router


class RlsNotEnforced(RuntimeError):
    """Startup refuses to continue when tenant isolation is not real."""


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Populate the registry and let every installed module contribute its
    # tables BEFORE migrating -- the same order tests/conftest.py uses.
    # Without this, the app boots with an empty registry: every /records
    # route 404s as "unknown entity" and the schema never gains a single
    # core or module table, because nothing else in server/ calls these.
    modules.install_enabled_modules()

    schema.migrate()

    problems = schema.verify_rls()
    if problems:
        raise RlsNotEnforced(f"row-level security is not fully armed: {problems}")

    health = pool.healthcheck()
    if not health.get("rls_enforced"):
        raise RlsNotEnforced(
            f"the application role {health.get('role')!r} is a superuser or has "
            "BYPASSRLS, which silently defeats every policy in the schema"
        )

    index_problems = schema.verify_index_leading_org()
    if index_problems:
        # A warning, not fatal: this is a performance cliff rather than a
        # correctness hole, and a deployment should not refuse to start over it.
        app.state.index_warnings = index_problems

    yield
    pool.close_pool()


app = FastAPI(title="CRM", version="0.1.0", lifespan=lifespan)

app.include_router(records.router)
app.include_router(records.association_router)
app.include_router(settings_api.router)
app.include_router(accounts_api.router)
app.include_router(channels_api.router)
app.include_router(proposals_api.router)
app.include_router(search_api.router)


# ── Errors ───────────────────────────────────────────────────────────────────
# Registered once here rather than repeated in every route body -- see
# server/api/records.py's module docstring, which relies on this mapping.

@app.exception_handler(repository.NotFound)
@app.exception_handler(registry.UnknownEntity)
def _not_found(request: Request, exc: Exception) -> Response:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


@app.exception_handler(permissions.PermissionDenied)
def _forbidden(request: Request, exc: Exception) -> Response:
    return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(exc)})


@app.exception_handler(repository.ValidationError)
@app.exception_handler(registry.UnknownField)
@app.exception_handler(query.FilterError)
@app.exception_handler(associations.AssociationError)
@app.exception_handler(core_settings.UnknownSection)
@app.exception_handler(llm_router.LLMError)
@app.exception_handler(embeddings_llm.EmbeddingError)
@app.exception_handler(account_link.LinkError)
def _bad_request(request: Request, exc: Exception) -> Response:
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


@app.exception_handler(account_link.LinkUpstreamError)
def _bad_gateway(request: Request, exc: Exception) -> Response:
    # A failure talking to the linked provider (Microsoft/Google), not a bad
    # request from our own caller -- 502 says "the upstream provider
    # failed," not "you sent something wrong." Registered against
    # `account_link.LinkUpstreamError` only -- never a provider module
    # directly (R2) -- because `microsoft_oauth.py`/`google_oauth.py` catch
    # `GraphError`/`GoogleApiError` at the dispatch boundary and re-raise
    # this instead; see account_link.py's own docstring.
    return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": str(exc)})


@app.exception_handler(repository.Conflict)
def _conflict(request: Request, exc: Exception) -> Response:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


# ── Schemas ──────────────────────────────────────────────────────────────────

class SetupRequest(BaseModel):
    org_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(default="", max_length=200)
    password: str = Field(min_length=passwords.MIN_PASSWORD_LENGTH, max_length=1024)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict[str, Any]:
    """Unauthenticated liveness. Reports whether tenant isolation is genuinely
    enforced, so a misconfigured role is visible from outside rather than only
    in a log line nobody reads."""
    try:
        db = pool.healthcheck()
    except Exception as exc:  # noqa: BLE001 -- health must answer, not 500
        return {"ok": False, "database": {"error": str(exc)}}
    return {
        "ok": True,
        "database": {
            "name": db.get("database"),
            "role": db.get("role"),
            "rls_enforced": db.get("rls_enforced"),
        },
        "first_run_required": auth.first_run_required(),
    }


# ── First run ────────────────────────────────────────────────────────────────

@app.get("/setup")
def setup_status() -> dict[str, Any]:
    return {
        "first_run_required": auth.first_run_required(),
        "setup_allowed": config.allow_first_run_setup(),
        "min_password_length": passwords.MIN_PASSWORD_LENGTH,
    }


@app.post("/setup", status_code=status.HTTP_201_CREATED)
def first_run_setup(body: SetupRequest, response: Response) -> dict[str, Any]:
    """Create the org and its first administrator, then log them in.

    Reachable only while no org exists (`guard_first_run` raises otherwise), so
    it cannot become a standing account-creation endpoint.
    """
    auth.guard_first_run()

    try:
        passwords.check_password_strength(body.password)
    except passwords.WeakPasswordError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    org = users.create_org(body.org_name)
    try:
        admin = users.create_user(
            str(org["id"]),
            email=body.email,
            name=body.name,
            password=body.password,
            is_admin=True,
        )
    except (ValueError, users.UserExistsError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    token, _ = sessions.create_session(str(org["id"]), str(admin["id"]))
    auth.set_session_cookie(response, token)
    return {
        "org": {"id": str(org["id"]), "name": org["name"]},
        "user": _public_user(admin),
        "token": token,
    }


# ── Session ──────────────────────────────────────────────────────────────────

@app.post("/auth/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    try:
        user = sessions.authenticate(body.email, body.password)
    except sessions.AuthenticationError as exc:
        # One message for every failure mode -- wrong password, unknown address,
        # disabled account. Distinguishing them turns login into an account
        # enumeration oracle.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    token, _ = sessions.create_session(
        str(user["org_id"]),
        str(user["id"]),
        user_agent=request.headers.get("user-agent", ""),
        ip=request.client.host if request.client else "",
    )
    auth.set_session_cookie(response, token)
    return {"user": _public_user(user), "token": token}


@app.post("/auth/logout")
def logout(
    response: Response,
    resolved: dict[str, Any] = Depends(auth.current_session),
) -> dict[str, Any]:
    sessions.revoke_session(str(resolved["user"]["org_id"]), resolved["session_id"])
    auth.clear_session_cookie(response)
    return {"ok": True}


@app.get("/auth/me")
def me(principal: Principal = Depends(auth.current_principal)) -> dict[str, Any]:
    """The caller's identity and merged effective permissions.

    The frontend renders from this rather than re-deriving anything, so what the
    UI shows and what the server enforces cannot drift apart.
    """
    user = users.get_user(principal.org_id, principal.user_id)
    if user is None:
        raise auth.UNAUTHENTICATED
    return {
        "user": _public_user(user),
        "is_admin": principal.is_admin,
        "permissions": {
            obj: {
                "can_create": perms.can_create,
                "read_level": perms.read_level,
                "edit_level": perms.edit_level,
                "delete_level": perms.delete_level,
                "field_masks": perms.field_masks,
            }
            for obj, perms in principal.permissions.items()
        },
    }


# ── Admin ────────────────────────────────────────────────────────────────────

@app.get("/users")
def list_users(
    principal: Principal = Depends(auth.require_admin_principal),
) -> dict[str, Any]:
    return {"users": [_public_user(u) for u in users.list_users(principal.org_id)]}


@app.get("/roles")
def list_roles(
    principal: Principal = Depends(auth.require_admin_principal),
) -> dict[str, Any]:
    return {
        "roles": [
            {"id": str(r["id"]), "name": r["name"], "description": r["description"]}
            for r in users.list_roles(principal.org_id)
        ],
        "levels": list(permissions.LEVELS),
        "actions": list(permissions.ACTIONS),
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    """The user shape the API returns. Never includes password_hash -- the
    allowlist here is why that cannot happen by forgetting to strip it."""
    return {
        "id": str(user["id"]),
        "org_id": str(user["org_id"]),
        "email": user.get("email_raw") or user.get("email"),
        "name": user.get("name") or "",
        "is_admin": bool(user.get("is_admin")),
        "status": user.get("status"),
        "team_id": str(user["team_id"]) if user.get("team_id") else None,
    }

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
from pydantic import BaseModel, Field

from server import config
from server.api import auth
from server.core import passwords, permissions, sessions, users
from server.core.permissions import Principal
from server.db import pool, schema


class RlsNotEnforced(RuntimeError):
    """Startup refuses to continue when tenant isolation is not real."""


@asynccontextmanager
async def lifespan(app: FastAPI):
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

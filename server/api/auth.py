"""FastAPI authentication: the request-scoped Principal, and the first-run gate.

Two dependencies:

  * `current_principal` -- resolves the session cookie or bearer token into a
    Principal with merged permissions. Everything behind auth depends on it.
  * `require_admin_principal` -- the same, then asserts the admin split.

## Fails closed

An unconfigured multi-user CRM refuses every request except the first-run setup
route, and that route stops working the instant an org exists. Cal fails OPEN
when unconfigured -- a deliberate choice there, so a single operator cannot lock
themselves out of the Settings page needed to configure auth -- and it is
explicitly not carried over. See docs/SOURCE-PATTERNS.md.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Cookie, Depends, Header, HTTPException, Request, status

from server import config
from server.core import permissions, sessions, users
from server.core.permissions import Principal

UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="authentication required",
    headers={"WWW-Authenticate": "Bearer"},
)


def _token_from(cookie_value: Optional[str], authorization: Optional[str]) -> Optional[str]:
    """Cookie first (the browser path), then `Authorization: Bearer` (the API
    and channel-worker path)."""
    if cookie_value:
        return cookie_value
    if authorization:
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() == "bearer" and credentials.strip():
            return credentials.strip()
    return None


async def current_session(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    token = _token_from(
        request.cookies.get(config.get_session_cookie_name()), authorization
    )
    resolved = sessions.resolve_session(token or "")
    if resolved is None:
        raise UNAUTHENTICATED
    return resolved


async def current_principal(
    resolved: dict[str, Any] = Depends(current_session),
) -> Principal:
    """The authenticated caller, with permissions already merged.

    Resolved once per request and passed down. Nothing below the API layer
    re-derives permissions, so there is exactly one place the model is applied.
    """
    return permissions.load_principal(resolved["user"])


async def require_admin_principal(
    principal: Principal = Depends(current_principal),
) -> Principal:
    """The admin half of the hard admin/non-admin split: settings, provider
    credentials, user management. Raises rather than degrading."""
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="administrator access required"
        )
    return principal


def first_run_required() -> bool:
    """True when nobody can log in yet -- the only condition under which the
    setup route is reachable.

    Asks auth.identities rather than core.orgs: a context-free connection
    correctly sees no tenant rows, so counting orgs would always return zero and
    leave setup permanently open. "Can anyone log in?" is also the more honest
    question, since an org with no users is not a configured system.
    """
    return users.count_identities() == 0


def guard_first_run() -> None:
    """Refuse the setup route once the system is configured, or when setup has
    been disabled outright.

    Raises rather than returning a flag: a caller that forgets to check a
    returned boolean would leave an open account-creation endpoint on a live
    deployment.
    """
    if not config.allow_first_run_setup():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="first-run setup is disabled"
        )
    if not first_run_required():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="already configured"
        )


def set_session_cookie(response, token: str) -> None:
    """HttpOnly so JavaScript cannot read it, SameSite=lax so it survives
    ordinary navigation but not cross-site form posts, Secure unless explicitly
    disabled for local plain-HTTP development."""
    response.set_cookie(
        key=config.get_session_cookie_name(),
        value=token,
        max_age=config.get_session_ttl_seconds(),
        httponly=True,
        secure=config.session_cookie_secure(),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(key=config.get_session_cookie_name(), path="/")

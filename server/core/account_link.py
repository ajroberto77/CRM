"""OAuth account-linking dispatch, shared by every axis a connected account
backs (contacts today, calendar in M5, potentially mail later) -- not itself
one of R3's six axes, since it is infrastructure underneath several of them,
the same status as `server/net/http_retry.py`.

`elif provider ==` with a lazy import per branch (this codebase's own R2/R3
convention, matching `server/providers/esign.py`), not a module-level
dict-of-impls -- consistency with the axis dispatchers already built here.

This module is also the ONE exception boundary for the link flow: adapters
(`microsoft_oauth.py`, `google_oauth.py`) never let a provider-specific
exception (`GraphError`, `GoogleApiError`, ...) escape past their own
`start_link`/`complete_link` -- they catch it and re-raise `LinkUpstreamError`
instead. That is what lets `server/api/app.py` register a handler for this
module's two exception classes only, never importing a provider module
directly -- otherwise adding a third provider would mean touching app.py too,
not just one new file plus one `elif` (R2).
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

from server.core import accounts


class LinkError(RuntimeError):
    pass


class LinkUpstreamError(LinkError):
    """The provider itself failed (token exchange, post-link profile lookup,
    missing client id/secret) -- as opposed to a caller error (unknown
    provider, expired state). A `LinkError` subclass so existing
    `except LinkError` / `pytest.raises(LinkError)` call sites still catch
    it; callers that need to tell the two apart (e.g. `app.py`'s 400 vs 502
    split) register a handler for this subclass specifically."""
    pass


def pkce_pair() -> tuple[str, str]:
    """One PKCE (code_verifier, code_challenge) pair -- RFC 7636 S256.
    Shared by every provider's `start_link` (`microsoft_oauth.py`,
    `google_oauth.py`); PKCE generation is generic OAuth2 mechanics, not
    provider-specific, so it lives at the dispatch layer once, not
    reimplemented per adapter (R1)."""
    verifier = base64.urlsafe_b64encode(os.urandom(40)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def start_link(org_id: str, account_id: str, provider: str, redirect_uri: str) -> str:
    # Opportunistic cleanup of expired PKCE state -- the schema's own
    # comment on `core.oauth_pending` promises this happens "on every new
    # link attempt"; a cron-only sweep would work too, but there is no
    # scheduler in this codebase yet and this table is never large enough
    # to need one.
    accounts.sweep_oauth_pending(org_id)
    if provider == "microsoft":
        import server.providers.microsoft_oauth as impl
    elif provider == "google":
        import server.providers.google_oauth as impl
    else:
        raise LinkError(f"unknown account provider {provider!r} (known: {', '.join(accounts.PROVIDERS)})")
    return impl.start_link(org_id, account_id, redirect_uri)


def complete_link(org_id: str, state: str, code: str, redirect_uri: str) -> dict[str, Any]:
    # The provider isn't known from `state` alone until the pending row is
    # read -- peek (non-destructive) here so dispatch can pick the right
    # adapter; the adapter itself does the real, single-use pop.
    pending = accounts.peek_oauth_pending(org_id, state)
    if pending is None:
        raise LinkError("unknown or expired oauth state")
    provider = pending["provider"]
    if provider == "microsoft":
        import server.providers.microsoft_oauth as impl
    elif provider == "google":
        import server.providers.google_oauth as impl
    else:
        raise LinkError(f"unknown account provider {provider!r} (known: {', '.join(accounts.PROVIDERS)})")
    return impl.complete_link(org_id, state, code, redirect_uri)

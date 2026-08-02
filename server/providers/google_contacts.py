"""Google People API contacts adapter. Delta query via `syncToken` -- the
sync cursor is Google's own opaque token, same discipline as Microsoft's
`deltaLink`.

A sync token can expire (Google returns 410 Gone) if too much time passes
between syncs. That is a recoverable condition, not a permanent failure
(safety rule 5): a full resync without a token repairs it, rather than
leaving the account permanently stuck.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from server.core import accounts
from server.providers import google_api as ga

_BASE = "https://people.googleapis.com/v1/people/me/connections"
_FIELDS = "names,emailAddresses,phoneNumbers,organizations"


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    if (raw.get("metadata") or {}).get("deleted"):
        return {"provider_object_id": raw["resourceName"], "deleted": True}
    names = raw.get("names") or []
    display_name = names[0].get("displayName", "") if names else ""
    emails = [e.get("value", "") for e in (raw.get("emailAddresses") or []) if e.get("value")]
    phones = [p.get("value", "") for p in (raw.get("phoneNumbers") or []) if p.get("value")]
    orgs = raw.get("organizations") or []
    company_name = orgs[0].get("name", "") if orgs else ""
    return {
        "provider_object_id": raw["resourceName"],
        "display_name": display_name,
        "emails": emails,
        "phones": phones,
        "company_name": company_name,
        "deleted": False,
    }


def _fetch(
    org_id: str, account_id: str, sync_token: str
) -> tuple[list[dict[str, Any]], str]:
    contacts: list[dict[str, Any]] = []
    next_cursor = sync_token
    page_token = ""
    while True:
        params = {"personFields": _FIELDS, "requestSyncToken": "true", "pageSize": "200"}
        if sync_token:
            params["syncToken"] = sync_token
        if page_token:
            params["pageToken"] = page_token
        url = f"{_BASE}?{urllib.parse.urlencode(params)}"
        page = ga.request("GET", url, org_id, account_id)
        contacts.extend(_normalize(raw) for raw in page.get("connections", []))
        next_sync_token = page.get("nextSyncToken")
        if next_sync_token:
            next_cursor = next_sync_token
        page_token = page.get("nextPageToken", "")
        if not page_token:
            break
    return contacts, next_cursor


def fetch_contacts(org_id: str, account: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    account_id = str(account["id"])
    sync_token = accounts.get_sync_cursor(org_id, account_id, "contacts")
    try:
        return _fetch(org_id, account_id, sync_token)
    except ga.GoogleApiError as exc:
        status = getattr(exc.__cause__, "status_code", None)
        if sync_token and status == 410:
            return _fetch(org_id, account_id, "")
        raise

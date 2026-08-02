"""Microsoft Graph contacts adapter (R2's `<provider>_contacts.py` for the
contacts axis). Delta query via `/me/contacts/delta` -- the sync cursor is
Graph's own opaque `@odata.deltaLink`, never interpreted by anything above
this file, same discipline as JA's opaque message ids.
"""
from __future__ import annotations

from typing import Any

from server.core import accounts
from server.providers import microsoft_graph as gc

_SELECT = "id,displayName,emailAddresses,businessPhones,homePhones,mobilePhone,companyName"


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    if "@removed" in raw:
        return {"provider_object_id": raw["id"], "deleted": True}
    emails = [
        e.get("address", "") for e in (raw.get("emailAddresses") or []) if e.get("address")
    ]
    phones = list(raw.get("businessPhones") or [])
    if raw.get("homePhones"):
        phones.extend(raw["homePhones"])
    if raw.get("mobilePhone"):
        phones.append(raw["mobilePhone"])
    return {
        "provider_object_id": raw["id"],
        "display_name": raw.get("displayName") or "",
        "emails": emails,
        "phones": phones,
        "company_name": raw.get("companyName") or "",
        "deleted": False,
    }


def fetch_contacts(org_id: str, account: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    account_id = str(account["id"])
    cursor = accounts.get_sync_cursor(org_id, account_id, "contacts")
    url = cursor or f"/me/contacts/delta?$select={_SELECT}"

    contacts: list[dict[str, Any]] = []
    next_cursor = cursor
    while url:
        page = gc.request("GET", url, org_id, account_id)
        contacts.extend(_normalize(raw) for raw in page.get("value", []))
        delta_link = page.get("@odata.deltaLink")
        if delta_link:
            next_cursor = delta_link
        url = page.get("@odata.nextLink")
    return contacts, next_cursor

"""R3's contacts axis dispatcher.

Generic core, name-prefixed adapters (R2): this module carries no vendor
name and never imports a vendor module except through `fetch_contacts()`
below. Adding a third provider is one new `<provider>_contacts.py` file
implementing `fetch_contacts()`, plus one `elif` here -- nothing else
changes.

Every adapter returns contacts already normalized into the one shared
shape (`provider_object_id`, `display_name`, `emails`, `phones`,
`company_name`, `deleted`) -- callers (`server/jobs/sync_poller.py`) never
branch on which vendor answered.

Cursor commit is generic (`server/core/accounts.py`'s `sync_cursors`
table) and lives here, not per-adapter -- there is nothing
provider-specific about persisting an opaque cursor string.
"""
from __future__ import annotations

from typing import Any

from server.core import accounts

PROVIDERS = ("microsoft", "google")


class ContactsError(RuntimeError):
    pass


def fetch_contacts(org_id: str, account: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Returns (contacts, next_cursor). Does NOT persist the cursor -- the
    caller commits it only once every contact in the batch has been
    resolved into a CRM record (safety rule 4: a transient failure must
    not lose data, so the cursor must never advance past a failed item)."""
    provider = account["provider"]
    if provider == "microsoft":
        import server.providers.microsoft_contacts as impl
    elif provider == "google":
        import server.providers.google_contacts as impl
    else:
        raise ContactsError(
            f"unknown contacts provider {provider!r} (known: {', '.join(PROVIDERS)})"
        )
    return impl.fetch_contacts(org_id, account)


def commit_cursor(org_id: str, account_id: str, cursor_token: str) -> None:
    accounts.commit_sync_cursor(org_id, account_id, "contacts", cursor_token)

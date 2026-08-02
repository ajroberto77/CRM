"""M4's contact sync loop -- a single standalone sleep loop, Cal's
`poll_loop.py` pattern verbatim, since M7's general work queue doesn't
exist yet and periodic polling across a small, fixed number of connected
accounts doesn't need one.

Whole-batch-retry (safety rule 4): a delta cursor only advances once every
contact in the fetched batch has been resolved into a CRM record. A
mid-batch failure leaves the cursor where it was, so the next cycle
re-fetches the same batch rather than silently skipping the failed item.

A provider failure (expired grant, network error, revoked consent) marks
the account `status='error'` but the account is still polled every cycle
after that (safety rule 5: no permanent latch on recoverable external
state) -- `status` flips back to `'active'` the moment a sync succeeds.

Run as its own process: `python3 -m server.jobs.sync_poller`.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from server.core import accounts, derivation, permissions, repository
from server.db import pool
from server.providers import contacts as contacts_dispatch

_DEFAULT_INTERVAL_SECONDS = 300


def _org_ids() -> list[str]:
    with pool.system_transaction() as cur:
        cur.execute("SELECT id FROM core.orgs")
        return [str(row["id"]) for row in cur.fetchall()]


def _primary_channel(contact: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    # A synced contact can carry several emails/phones; only the first is
    # treated as the identity signal. Resolving every value independently
    # risks materializing a separate derived person per value that hasn't
    # been seen before, with no way to merge them back into one -- the same
    # class of gap `derivation.py`'s own docstring already accepts for its
    # simpler single-channel case. Primary-email-is-canonical is the
    # deliberate, documented scope limit for this pass.
    emails = contact.get("emails") or []
    if emails:
        return "email", emails[0]
    phones = contact.get("phones") or []
    if phones:
        return "phone", phones[0]
    return None, None


def _delete_sync_link(org_id: str, account_id: str, provider_object_id: str) -> None:
    link = accounts.get_sync_link(org_id, account_id, provider_object_id)
    if link is None:
        return
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "DELETE FROM core.sync_links WHERE org_id = %s AND id = %s",
            (org_id, link["id"]),
        )


def _sync_one_contact(principal, account: dict[str, Any], contact: dict[str, Any]) -> None:
    org_id = str(account["org_id"])
    account_id = str(account["id"])
    provider_object_id = contact["provider_object_id"]

    if contact.get("deleted"):
        # Not mirrored as a CRM delete -- a derived person may have
        # accrued real human edits (notes, deals) since it was
        # materialized, and a provider-side delete silently taking those
        # with it would be a real data-loss surprise. Dropping the link
        # just means a re-added contact re-syncs fresh.
        _delete_sync_link(org_id, account_id, provider_object_id)
        return

    channel_kind, raw_value = _primary_channel(contact)
    if raw_value is None:
        return  # no email or phone on this contact -- nothing to resolve against

    person_id = derivation.resolve_or_create_person(principal, channel_kind, raw_value)
    if person_id is None:
        return  # value didn't normalize (e.g. a malformed phone) -- nothing to link

    display_name = (contact.get("display_name") or "").strip()
    if display_name:
        person = repository.get_record(principal, "person", person_id)
        # Only upgrade a still-derived placeholder name -- never overwrite
        # what a human has already promoted by editing (derivation.py's own
        # promotion rule: a human edit wins over sync, permanently).
        if person.get("is_derived") and person.get("full_name") != display_name:
            repository.update(principal, "person", person_id, {"full_name": display_name})

    accounts.create_sync_link(org_id, account_id, provider_object_id, "person", person_id)


def sync_account(account: dict[str, Any]) -> bool:
    """Returns True if the whole batch synced cleanly."""
    org_id = str(account["org_id"])
    account_id = str(account["id"])
    principal = permissions.system_principal(org_id, "contact sync")

    try:
        contact_list, next_cursor = contacts_dispatch.fetch_contacts(org_id, account)
    except Exception as exc:  # noqa: BLE001 -- a provider failure must not crash the poller
        accounts.update_account_status(org_id, account_id, "error", str(exc)[:500])
        return False

    for contact in contact_list:
        try:
            _sync_one_contact(principal, account, contact)
        except Exception as exc:  # noqa: BLE001
            accounts.update_account_status(org_id, account_id, "error", str(exc)[:500])
            return False  # stop here -- the cursor must not advance past this contact

    contacts_dispatch.commit_cursor(org_id, account_id, next_cursor)
    if account["status"] == "error":
        accounts.update_account_status(org_id, account_id, "active")
    return True


def poll_once() -> None:
    for org_id in _org_ids():
        for account in accounts.list_accounts(org_id):
            if account["status"] not in ("active", "error"):
                continue  # 'pending' (link never completed) / 'disabled' -- skip
            sync_account(account)


def run_forever(interval_seconds: int = _DEFAULT_INTERVAL_SECONDS) -> None:
    while True:
        poll_once()
        time.sleep(interval_seconds)


if __name__ == "__main__":
    run_forever()

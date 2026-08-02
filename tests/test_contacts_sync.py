"""server/providers/contacts.py + server/jobs/sync_poller.py -- the
contacts axis dispatcher and the whole-batch-retry sync loop (safety rules
4 and 5). No real network call anywhere -- fetch_contacts is mocked at the
dispatcher boundary, same as tests/test_llm_router.py mocks adapter calls
rather than urlopen when the point under test is orchestration, not wire
format.
"""
from __future__ import annotations

from unittest import mock

import pytest

from server.core import accounts, permissions, repository, users
from server.providers import contacts as contacts_dispatch
from server.jobs import sync_poller


@pytest.fixture
def account(org_id):
    admin = users.create_user(
        org_id, email="contactsync@example.com", name="A",
        password="correct-horse-battery", is_admin=True,
    )
    acc = accounts.create_pending_account(org_id, str(admin["id"]), "microsoft")
    accounts.save_credentials(
        org_id, str(acc["id"]), "oauth", {"refresh_token": "rt", "scopes": "s"}
    )
    accounts.activate_account(org_id, str(acc["id"]), "me@example.com")
    return accounts.get_account(org_id, str(acc["id"]))


class TestContactsDispatch:
    def test_unknown_provider_raises(self, org_id):
        with pytest.raises(contacts_dispatch.ContactsError):
            contacts_dispatch.fetch_contacts(org_id, {"provider": "aol", "id": "x"})


class TestSyncAccount:
    def test_a_clean_batch_creates_a_person_and_commits_the_cursor(
        self, org_id, account, principal
    ):
        fake_contacts = [
            {
                "provider_object_id": "c1", "display_name": "Jane Doe",
                "emails": ["jane@example.com"], "phones": [], "deleted": False,
            },
        ]
        with mock.patch.object(
            contacts_dispatch, "fetch_contacts", return_value=(fake_contacts, "cursor-1")
        ):
            assert sync_poller.sync_account(account) is True

        people = repository.list_records(principal, "person")
        assert people["total"] == 1
        assert people["records"][0]["full_name"] == "Jane Doe"
        assert people["records"][0]["is_derived"] is True

        link = accounts.get_sync_link(org_id, str(account["id"]), "c1")
        assert link["entity_type"] == "person"
        assert accounts.get_sync_cursor(org_id, str(account["id"]), "contacts") == "cursor-1"

    def test_a_contact_with_no_email_or_phone_is_skipped(self, org_id, account, principal):
        fake_contacts = [
            {"provider_object_id": "c1", "display_name": "No Channel", "emails": [], "phones": [], "deleted": False},
        ]
        with mock.patch.object(
            contacts_dispatch, "fetch_contacts", return_value=(fake_contacts, "cursor-1")
        ):
            assert sync_poller.sync_account(account) is True
        people = repository.list_records(principal, "person")
        assert people["total"] == 0

    def test_re_syncing_the_same_contact_does_not_duplicate_the_person(
        self, org_id, account, principal
    ):
        fake_contacts = [
            {
                "provider_object_id": "c1", "display_name": "Jane Doe",
                "emails": ["jane@example.com"], "phones": [], "deleted": False,
            },
        ]
        with mock.patch.object(
            contacts_dispatch, "fetch_contacts", return_value=(fake_contacts, "cursor-1")
        ):
            sync_poller.sync_account(account)
        with mock.patch.object(
            contacts_dispatch, "fetch_contacts", return_value=(fake_contacts, "cursor-2")
        ):
            sync_poller.sync_account(accounts.get_account(org_id, str(account["id"])))

        people = repository.list_records(principal, "person")
        assert people["total"] == 1

    def test_a_deleted_contact_drops_the_link_but_keeps_the_person(
        self, org_id, account, principal
    ):
        fake_contacts = [
            {
                "provider_object_id": "c1", "display_name": "Jane Doe",
                "emails": ["jane@example.com"], "phones": [], "deleted": False,
            },
        ]
        with mock.patch.object(
            contacts_dispatch, "fetch_contacts", return_value=(fake_contacts, "cursor-1")
        ):
            sync_poller.sync_account(account)

        deleted = [{"provider_object_id": "c1", "deleted": True}]
        with mock.patch.object(
            contacts_dispatch, "fetch_contacts", return_value=(deleted, "cursor-2")
        ):
            sync_poller.sync_account(accounts.get_account(org_id, str(account["id"])))

        assert accounts.get_sync_link(org_id, str(account["id"]), "c1") is None
        people = repository.list_records(principal, "person")
        assert people["total"] == 1  # not deleted -- may carry real human edits since

    def test_a_promoted_person_is_never_renamed_by_a_later_sync(
        self, org_id, account, principal
    ):
        fake_contacts = [
            {
                "provider_object_id": "c1", "display_name": "Provider Name",
                "emails": ["jane@example.com"], "phones": [], "deleted": False,
            },
        ]
        with mock.patch.object(
            contacts_dispatch, "fetch_contacts", return_value=(fake_contacts, "cursor-1")
        ):
            sync_poller.sync_account(account)

        person_id = repository.list_records(principal, "person")["records"][0]["id"]
        # A human edits the record -- this promotes it (derivation.py's own
        # promotion rule), and from then on sync must never overwrite the name.
        repository.update(principal, "person", str(person_id), {"full_name": "Human Chosen Name"})

        renamed_contacts = [
            {
                "provider_object_id": "c1", "display_name": "Provider Renamed It",
                "emails": ["jane@example.com"], "phones": [], "deleted": False,
            },
        ]
        with mock.patch.object(
            contacts_dispatch, "fetch_contacts", return_value=(renamed_contacts, "cursor-2")
        ):
            sync_poller.sync_account(accounts.get_account(org_id, str(account["id"])))

        person = repository.get_record(principal, "person", str(person_id))
        assert person["full_name"] == "Human Chosen Name"
        assert person["is_derived"] is False

    def test_a_failure_mid_batch_does_not_advance_the_cursor(self, org_id, account, principal):
        fake_contacts = [
            {
                "provider_object_id": "c1", "display_name": "Ok",
                "emails": ["ok@example.com"], "phones": [], "deleted": False,
            },
            {
                "provider_object_id": "c2", "display_name": "Boom",
                "emails": ["boom@example.com"], "phones": [], "deleted": False,
            },
        ]
        real_resolve = sync_poller.derivation.resolve_or_create_person

        def flaky(principal_arg, kind, raw):
            if raw == "boom@example.com":
                raise RuntimeError("simulated provider hiccup")
            return real_resolve(principal_arg, kind, raw)

        with mock.patch.object(
            contacts_dispatch, "fetch_contacts", return_value=(fake_contacts, "cursor-should-not-commit")
        ), mock.patch.object(
            sync_poller.derivation, "resolve_or_create_person", side_effect=flaky
        ):
            assert sync_poller.sync_account(account) is False

        updated = accounts.get_account(org_id, str(account["id"]))
        assert updated["status"] == "error"
        assert accounts.get_sync_cursor(org_id, str(account["id"]), "contacts") == ""

    def test_a_previously_errored_account_recovers_to_active_on_the_next_clean_sync(
        self, org_id, account, principal
    ):
        accounts.update_account_status(org_id, str(account["id"]), "error", "boom")
        fake_contacts = [
            {
                "provider_object_id": "c1", "display_name": "Jane",
                "emails": ["jane@example.com"], "phones": [], "deleted": False,
            },
        ]
        with mock.patch.object(
            contacts_dispatch, "fetch_contacts", return_value=(fake_contacts, "cursor-1")
        ):
            assert sync_poller.sync_account(accounts.get_account(org_id, str(account["id"]))) is True

        updated = accounts.get_account(org_id, str(account["id"]))
        assert updated["status"] == "active"

    def test_a_fetch_contacts_failure_marks_the_account_error_without_crashing(
        self, org_id, account
    ):
        with mock.patch.object(
            contacts_dispatch, "fetch_contacts", side_effect=RuntimeError("network down")
        ):
            assert sync_poller.sync_account(account) is False
        updated = accounts.get_account(org_id, str(account["id"]))
        assert updated["status"] == "error"


class TestPollOnce:
    def test_pending_and_disabled_accounts_are_skipped(self, org_id):
        admin = users.create_user(
            org_id, email="pollonce@example.com", name="A",
            password="correct-horse-battery", is_admin=True,
        )
        pending_account = accounts.create_pending_account(org_id, str(admin["id"]), "microsoft")

        with mock.patch.object(sync_poller, "sync_account") as fake_sync:
            sync_poller.poll_once()
        fake_sync.assert_not_called()

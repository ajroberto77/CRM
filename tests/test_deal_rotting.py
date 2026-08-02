"""M7's deal rotting -- a computed field (server/core/registry.py's
`_deal_rotting`/`_deal_rotting_context`), kept fed by
server/core/deal_activity.py's last_activity_at subscriber. No job
anywhere: this is pure read-time arithmetic against real DB state.
"""
from __future__ import annotations

import pytest

from server.core import repository, settings
from server.db import pool


def _backdate(org_id, deal_id, days):
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "UPDATE core.deals SET last_activity_at = now() - (%s || ' days')::interval "
            "WHERE id = %s",
            (days, deal_id),
        )


class TestFreshDeal:
    def test_a_brand_new_deal_is_never_rotting(self, org_id, principal):
        deal = repository.create(principal, "deal", {"name": "Acme", "status": "open"})
        assert deal["rotting"] is False
        assert deal["last_activity_at"] is None


class TestThreshold:
    def test_older_than_the_default_threshold_is_rotting(self, org_id, principal):
        deal = repository.create(principal, "deal", {"name": "Acme", "status": "open"})
        _backdate(org_id, str(deal["id"]), 30)
        fetched = repository.get_record(principal, "deal", str(deal["id"]))
        assert fetched["rotting"] is True

    def test_within_the_default_threshold_is_not_rotting(self, org_id, principal):
        deal = repository.create(principal, "deal", {"name": "Acme", "status": "open"})
        _backdate(org_id, str(deal["id"]), 5)
        fetched = repository.get_record(principal, "deal", str(deal["id"]))
        assert fetched["rotting"] is False

    def test_the_threshold_is_configurable_per_org(self, org_id, principal):
        deal = repository.create(principal, "deal", {"name": "Acme", "status": "open"})
        _backdate(org_id, str(deal["id"]), 30)
        settings.update_settings(org_id, "pipeline", {"rotting_threshold_days": 45})
        fetched = repository.get_record(principal, "deal", str(deal["id"]))
        assert fetched["rotting"] is False

    def test_lowering_the_threshold_can_make_a_deal_start_rotting(self, org_id, principal):
        deal = repository.create(principal, "deal", {"name": "Acme", "status": "open"})
        _backdate(org_id, str(deal["id"]), 10)
        settings.update_settings(org_id, "pipeline", {"rotting_threshold_days": 5})
        fetched = repository.get_record(principal, "deal", str(deal["id"]))
        assert fetched["rotting"] is True


class TestClosedDealsNeverRot:
    def test_a_won_deal_is_never_rotting_regardless_of_staleness(self, org_id, principal):
        deal = repository.create(principal, "deal", {"name": "Acme", "status": "open"})
        _backdate(org_id, str(deal["id"]), 90)
        repository.update(principal, "deal", str(deal["id"]), {"status": "won"})
        fetched = repository.get_record(principal, "deal", str(deal["id"]))
        assert fetched["rotting"] is False

    def test_a_lost_deal_is_never_rotting(self, org_id, principal):
        deal = repository.create(principal, "deal", {"name": "Acme", "status": "open"})
        _backdate(org_id, str(deal["id"]), 90)
        repository.update(principal, "deal", str(deal["id"]), {"status": "lost"})
        fetched = repository.get_record(principal, "deal", str(deal["id"]))
        assert fetched["rotting"] is False


class TestListingIncludesRotting:
    def test_every_row_in_a_list_response_carries_the_field(self, org_id, principal):
        repository.create(principal, "deal", {"name": "Acme", "status": "open"})
        repository.create(principal, "deal", {"name": "Globex", "status": "open"})
        listing = repository.list_records(principal, "deal")
        assert listing["total"] == 2
        assert all("rotting" in record for record in listing["records"])


class TestRottingIsNotWritable:
    def test_a_human_cannot_write_rotting_directly(self, org_id, principal):
        deal = repository.create(principal, "deal", {"name": "Acme", "status": "open"})
        with pytest.raises(Exception):
            repository.update(principal, "deal", str(deal["id"]), {"rotting": True})


class TestLastActivitySubscriber:
    def test_a_human_edit_to_the_deal_updates_last_activity_at(self, org_id, principal):
        deal = repository.create(principal, "deal", {"name": "Acme", "status": "open"})
        assert deal["last_activity_at"] is None
        repository.update(principal, "deal", str(deal["id"]), {"stage": "negotiation"})
        fetched = repository.get_record(principal, "deal", str(deal["id"]))
        assert fetched["last_activity_at"] is not None

    def test_creating_a_task_against_the_deal_updates_last_activity_at(self, org_id, principal):
        deal = repository.create(principal, "deal", {"name": "Acme", "status": "open"})
        repository.create(
            principal, "task",
            {"title": "Call back", "subject_type": "deal", "subject_id": str(deal["id"])},
        )
        fetched = repository.get_record(principal, "deal", str(deal["id"]))
        assert fetched["last_activity_at"] is not None

    def test_creating_a_note_against_the_deal_updates_last_activity_at(self, org_id, principal):
        deal = repository.create(principal, "deal", {"name": "Acme", "status": "open"})
        repository.create(
            principal, "note", {"body": "Great call", "subject_type": "deal", "subject_id": str(deal["id"])},
        )
        fetched = repository.get_record(principal, "deal", str(deal["id"]))
        assert fetched["last_activity_at"] is not None

    def test_a_task_against_an_unrelated_subject_does_not_touch_any_deal(self, org_id, principal):
        deal = repository.create(principal, "deal", {"name": "Acme", "status": "open"})
        person = repository.create(principal, "person", {"full_name": "Jane Doe"})
        repository.create(
            principal, "task",
            {"title": "Call back", "subject_type": "person", "subject_id": str(person["id"])},
        )
        fetched = repository.get_record(principal, "deal", str(deal["id"]))
        assert fetched["last_activity_at"] is None

    def test_the_subscribers_own_bookkeeping_write_does_not_recurse_forever(self, org_id, principal):
        """A guard bug here would hang the test (or blow events.MAX_DEPTH
        loudly) rather than fail an assertion -- the test passing at all,
        promptly, is the coverage."""
        deal = repository.create(principal, "deal", {"name": "Acme", "status": "open"})
        repository.update(principal, "deal", str(deal["id"]), {"stage": "negotiation"})
        fetched = repository.get_record(principal, "deal", str(deal["id"]))
        assert fetched is not None  # reached without hanging/raising

"""server/core/proposals.py -- the one approval queue."""
from __future__ import annotations

import uuid

import pytest

from server.core import proposals, repository


class TestCreate:
    def test_create_defaults_to_pending(self, org_id):
        p = proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event",
            payload={"title": "Dentist"}, confidence=0.9, category="medical",
        )
        assert p["status"] == "pending"
        assert p["payload"] == {"title": "Dentist"}
        assert p["decided_by"] == ""
        assert p["decided_at"] is None

    def test_owner_id_is_stored_when_given(self, org_id, admin):
        p = proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event",
            payload={}, owner_id=str(admin["id"]),
        )
        assert str(p["owner_id"]) == str(admin["id"])


class TestGenericPathBlocksMutation:
    def test_generic_update_cannot_change_status(self, org_id, principal):
        p = proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event", payload={}
        )
        with pytest.raises(Exception):
            repository.update(principal, "proposed_change", str(p["id"]), {"status": "approved"})

    def test_generic_create_is_still_reachable_for_browsing(self, org_id, principal):
        proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event", payload={}
        )
        listing = repository.list_records(principal, "proposed_change")
        assert listing["total"] == 1


class TestApproveDecline:
    def test_approve_sets_status_and_decided_by(self, org_id):
        p = proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event", payload={}
        )
        approved = proposals.approve(org_id, str(p["id"]), decided_by="dashboard")
        assert approved["status"] == "approved"
        assert approved["decided_by"] == "dashboard"
        assert approved["decided_at"] is not None

    def test_decline_sets_status(self, org_id):
        p = proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event", payload={}
        )
        declined = proposals.decline(org_id, str(p["id"]))
        assert declined["status"] == "declined"

    def test_auto_approve_records_the_reason_as_decided_by(self, org_id):
        p = proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event", payload={}
        )
        result = proposals.auto_approve(org_id, str(p["id"]), "auto:mode")
        assert result["status"] == "auto_approved"
        assert result["decided_by"] == "auto:mode"

    def test_double_decision_raises_already_decided(self, org_id):
        p = proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event", payload={}
        )
        proposals.approve(org_id, str(p["id"]))
        with pytest.raises(proposals.AlreadyDecidedError):
            proposals.approve(org_id, str(p["id"]))
        with pytest.raises(proposals.AlreadyDecidedError):
            proposals.decline(org_id, str(p["id"]))

    def test_approve_then_decline_is_also_rejected(self, org_id):
        p = proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event", payload={}
        )
        proposals.approve(org_id, str(p["id"]))
        with pytest.raises(proposals.AlreadyDecidedError):
            proposals.decline(org_id, str(p["id"]))


class TestAllDecidedFor:
    def test_false_while_any_proposal_is_pending(self, org_id):
        subject_id = str(uuid.uuid4())
        proposals.create(
            org_id, subject_type="msg", subject_id=subject_id, kind="x", payload={}
        )
        assert proposals.all_decided_for(org_id, "msg", subject_id) is False

    def test_true_once_every_proposal_is_decided(self, org_id):
        subject_id = str(uuid.uuid4())
        p1 = proposals.create(org_id, subject_type="msg", subject_id=subject_id, kind="x", payload={})
        p2 = proposals.create(org_id, subject_type="msg", subject_id=subject_id, kind="x", payload={})
        proposals.approve(org_id, str(p1["id"]))
        assert proposals.all_decided_for(org_id, "msg", subject_id) is False
        proposals.decline(org_id, str(p2["id"]))
        assert proposals.all_decided_for(org_id, "msg", subject_id) is True

    def test_true_when_there_are_no_proposals_at_all(self, org_id):
        assert proposals.all_decided_for(org_id, "msg", str(uuid.uuid4())) is True


class TestNotificationMessageId:
    def test_set_and_read_back(self, org_id):
        p = proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event", payload={}
        )
        proposals.set_notification_message_id(org_id, str(p["id"]), "signal-msg-123")
        assert proposals.get(org_id, str(p["id"]))["notification_message_id"] == "signal-msg-123"

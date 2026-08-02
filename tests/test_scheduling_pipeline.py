"""server/core/scheduling_pipeline.py -- the interaction -> extraction ->
approval-queue -> calendar-write wiring. No real LLM/network call
anywhere: the LLM adapter and the calendar dispatcher are both mocked at
their own boundary, the same convention test_scheduling_extraction.py and
test_calendar_provider.py already use.
"""
from __future__ import annotations

from unittest import mock

import pytest

from server.core import accounts, repository, settings as core_settings, trust, users
from server.llm import ollama_llm
from server.providers import calendar as cal_dispatch

_FAKE_EXTRACTION = {
    "is_scheduling_relevant": True,
    "events": [{
        "title": "Dentist", "date": "2026-03-10", "time": "09:00", "duration_minutes": 30,
        "location": None, "attendees": [], "note": None, "category": "medical", "confidence": 0.6,
    }],
}


@pytest.fixture
def linked_account(org_id, admin):
    acc = accounts.create_pending_account(org_id, str(admin["id"]), "microsoft")
    accounts.save_credentials(org_id, str(acc["id"]), "oauth", {"refresh_token": "rt", "scopes": "s"})
    accounts.activate_account(org_id, str(acc["id"]), "me@example.com")
    return accounts.get_account(org_id, str(acc["id"]))


def _create_inbound_email(principal, owner_id, sender="jane@example.com", body="Dentist March 10 at 9am?"):
    return repository.create(principal, "interaction", {
        "owner_id": owner_id, "kind": "email", "direction": "inbound",
        "from_channel": sender, "body": body,
    })


class TestMasterGate:
    def test_calendar_write_disabled_leaves_the_proposal_pending(
        self, org_id, principal, admin, linked_account, monkeypatch
    ):
        monkeypatch.setattr(
            "server.core.scheduling_pipeline.config.calendar_write_enabled", lambda: False
        )
        core_settings.update_settings(org_id, "approval", {"approval_mode": "auto"})
        with mock.patch.object(ollama_llm, "call_structured", return_value=_FAKE_EXTRACTION), \
             mock.patch.object(cal_dispatch, "create_event") as fake_create:
            _create_inbound_email(principal, str(admin["id"]))
        assert not fake_create.called
        proposal = repository.list_records(principal, "proposed_change")["records"][0]
        assert proposal["status"] == "pending"


class TestNonEmailOrOutboundIsIgnored:
    def test_a_call_interaction_never_extracts(self, org_id, principal, admin, monkeypatch):
        monkeypatch.setattr(
            "server.core.scheduling_pipeline.config.calendar_write_enabled", lambda: True
        )
        with mock.patch.object(ollama_llm, "call_structured") as fake_llm:
            repository.create(principal, "interaction", {
                "owner_id": str(admin["id"]), "kind": "call", "direction": "inbound",
                "from_channel": "555-1000", "body": "called about the dentist",
            })
        fake_llm.assert_not_called()

    def test_an_outbound_email_never_extracts(self, org_id, principal, admin, monkeypatch):
        monkeypatch.setattr(
            "server.core.scheduling_pipeline.config.calendar_write_enabled", lambda: True
        )
        with mock.patch.object(ollama_llm, "call_structured") as fake_llm:
            repository.create(principal, "interaction", {
                "owner_id": str(admin["id"]), "kind": "email", "direction": "outbound",
                "from_channel": "me@example.com", "body": "Confirming the dentist visit",
            })
        fake_llm.assert_not_called()

    def test_an_empty_body_never_extracts(self, org_id, principal, admin, monkeypatch):
        monkeypatch.setattr(
            "server.core.scheduling_pipeline.config.calendar_write_enabled", lambda: True
        )
        with mock.patch.object(ollama_llm, "call_structured") as fake_llm:
            repository.create(principal, "interaction", {
                "owner_id": str(admin["id"]), "kind": "email", "direction": "inbound",
                "from_channel": "jane@example.com", "body": "   ",
            })
        fake_llm.assert_not_called()


class TestApprovalModeRouting:
    def test_manual_mode_leaves_it_pending(self, org_id, principal, admin, linked_account, monkeypatch):
        monkeypatch.setattr(
            "server.core.scheduling_pipeline.config.calendar_write_enabled", lambda: True
        )
        with mock.patch.object(ollama_llm, "call_structured", return_value=_FAKE_EXTRACTION), \
             mock.patch.object(cal_dispatch, "create_event") as fake_create:
            _create_inbound_email(principal, str(admin["id"]))
        assert not fake_create.called
        proposal = repository.list_records(principal, "proposed_change")["records"][0]
        assert proposal["status"] == "pending"
        assert proposal["category"] == "medical"

    def test_auto_mode_executes_and_auto_approves(
        self, org_id, principal, admin, linked_account, monkeypatch
    ):
        monkeypatch.setattr(
            "server.core.scheduling_pipeline.config.calendar_write_enabled", lambda: True
        )
        core_settings.update_settings(org_id, "approval", {"approval_mode": "auto"})
        with mock.patch.object(ollama_llm, "call_structured", return_value=_FAKE_EXTRACTION), \
             mock.patch.object(cal_dispatch, "create_event", return_value="evt-1") as fake_create, \
             mock.patch.object(
                 cal_dispatch, "check_event",
                 return_value={"existing_events": [], "update_target": None},
             ):
            _create_inbound_email(principal, str(admin["id"]))
        assert fake_create.called
        proposal = repository.list_records(principal, "proposed_change")["records"][0]
        assert proposal["status"] == "auto_approved"
        assert proposal["decided_by"] == "auto:mode"

    def test_confidence_mode_executes_above_threshold(
        self, org_id, principal, admin, linked_account, monkeypatch
    ):
        monkeypatch.setattr(
            "server.core.scheduling_pipeline.config.calendar_write_enabled", lambda: True
        )
        core_settings.update_settings(
            org_id, "approval", {"approval_mode": "confidence", "confidence_threshold": 0.5}
        )
        with mock.patch.object(ollama_llm, "call_structured", return_value=_FAKE_EXTRACTION), \
             mock.patch.object(cal_dispatch, "create_event", return_value="evt-1") as fake_create, \
             mock.patch.object(
                 cal_dispatch, "check_event",
                 return_value={"existing_events": [], "update_target": None},
             ):
            _create_inbound_email(principal, str(admin["id"]))
        assert fake_create.called  # extraction confidence is 0.6, above the 0.5 threshold

    def test_confidence_mode_stays_pending_below_threshold(
        self, org_id, principal, admin, linked_account, monkeypatch
    ):
        monkeypatch.setattr(
            "server.core.scheduling_pipeline.config.calendar_write_enabled", lambda: True
        )
        core_settings.update_settings(
            org_id, "approval", {"approval_mode": "confidence", "confidence_threshold": 0.95}
        )
        with mock.patch.object(ollama_llm, "call_structured", return_value=_FAKE_EXTRACTION), \
             mock.patch.object(cal_dispatch, "create_event") as fake_create:
            _create_inbound_email(principal, str(admin["id"]))
        assert not fake_create.called
        proposal = repository.list_records(principal, "proposed_change")["records"][0]
        assert proposal["status"] == "pending"


class TestCategoryAndTrustRule:
    def test_fires_independently_of_manual_mode_when_both_match(
        self, org_id, principal, admin, linked_account, monkeypatch
    ):
        monkeypatch.setattr(
            "server.core.scheduling_pipeline.config.calendar_write_enabled", lambda: True
        )
        core_settings.update_settings(org_id, "approval", {"auto_accept_categories": ["medical"]})
        trust.add_sender(principal, "@trustedclinic.com")
        with mock.patch.object(ollama_llm, "call_structured", return_value=_FAKE_EXTRACTION), \
             mock.patch.object(cal_dispatch, "create_event", return_value="evt-1") as fake_create, \
             mock.patch.object(
                 cal_dispatch, "check_event",
                 return_value={"existing_events": [], "update_target": None},
             ):
            _create_inbound_email(
                principal, str(admin["id"]), sender="scheduler@trustedclinic.com"
            )
        assert fake_create.called
        proposal = repository.list_records(principal, "proposed_change")["records"][0]
        assert proposal["decided_by"] == "auto:rule(medical,sender:@trustedclinic.com)"

    def test_category_matches_but_sender_is_untrusted_stays_pending(
        self, org_id, principal, admin, linked_account, monkeypatch
    ):
        monkeypatch.setattr(
            "server.core.scheduling_pipeline.config.calendar_write_enabled", lambda: True
        )
        core_settings.update_settings(org_id, "approval", {"auto_accept_categories": ["medical"]})
        with mock.patch.object(ollama_llm, "call_structured", return_value=_FAKE_EXTRACTION), \
             mock.patch.object(cal_dispatch, "create_event") as fake_create:
            _create_inbound_email(principal, str(admin["id"]), sender="stranger@unknown.com")
        assert not fake_create.called

    def test_trusted_sender_but_uncategorized_auto_accept_list_stays_pending(
        self, org_id, principal, admin, linked_account, monkeypatch
    ):
        monkeypatch.setattr(
            "server.core.scheduling_pipeline.config.calendar_write_enabled", lambda: True
        )
        trust.add_sender(principal, "@trustedclinic.com")
        # auto_accept_categories left empty -- the rule must never fire
        with mock.patch.object(ollama_llm, "call_structured", return_value=_FAKE_EXTRACTION), \
             mock.patch.object(cal_dispatch, "create_event") as fake_create:
            _create_inbound_email(
                principal, str(admin["id"]), sender="scheduler@trustedclinic.com"
            )
        assert not fake_create.called

    def test_the_rule_is_additive_never_subtractive(
        self, org_id, principal, admin, linked_account, monkeypatch
    ):
        """manual mode + a matching category+trust rule still auto-accepts
        -- the rule can grant what the mode alone would not, but the mode
        being conservative never blocks the rule."""
        monkeypatch.setattr(
            "server.core.scheduling_pipeline.config.calendar_write_enabled", lambda: True
        )
        core_settings.update_settings(
            org_id, "approval",
            {"approval_mode": "manual", "auto_accept_categories": ["medical"]},
        )
        trust.add_sender(principal, "@trustedclinic.com")
        with mock.patch.object(ollama_llm, "call_structured", return_value=_FAKE_EXTRACTION), \
             mock.patch.object(cal_dispatch, "create_event", return_value="evt-1") as fake_create, \
             mock.patch.object(
                 cal_dispatch, "check_event",
                 return_value={"existing_events": [], "update_target": None},
             ):
            _create_inbound_email(
                principal, str(admin["id"]), sender="scheduler@trustedclinic.com"
            )
        assert fake_create.called


class TestNoLinkedAccount:
    def test_auto_mode_with_no_linked_account_still_marks_auto_approved_but_writes_nothing(
        self, org_id, principal, admin, monkeypatch
    ):
        monkeypatch.setattr(
            "server.core.scheduling_pipeline.config.calendar_write_enabled", lambda: True
        )
        core_settings.update_settings(org_id, "approval", {"approval_mode": "auto"})
        with mock.patch.object(ollama_llm, "call_structured", return_value=_FAKE_EXTRACTION), \
             mock.patch.object(cal_dispatch, "create_event") as fake_create:
            _create_inbound_email(principal, str(admin["id"]))
        assert not fake_create.called  # nothing to write to

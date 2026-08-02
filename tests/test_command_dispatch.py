"""server/channels/commands.py -- the command grammar. Two invariants get
their own dedicated tests here (see the module's own docstring): a quote
that doesn't resolve stops cold rather than falling back to "most recent
pending," and no code path can reach an LLM call.
"""
from __future__ import annotations

from unittest import mock

import pytest

from server.channels import commands
from server.core import proposals
from server.llm import ollama_llm


def _pending_proposal(org_id, owner_id):
    return proposals.create(
        org_id, subject_type="interaction", subject_id=None, kind="calendar_event",
        payload={"title": "Dentist"}, owner_id=owner_id,
    )


def _notified(org_id, proposal, message_id="signal-msg-1"):
    proposals.set_notification_message_id(org_id, str(proposal["id"]), message_id)
    return message_id


class TestApprove:
    def test_yes_quoting_the_notification_approves_it(self, org_id, admin):
        proposal = _pending_proposal(org_id, str(admin["id"]))
        message_id = _notified(org_id, proposal)
        handled = commands.dispatch_command(org_id, str(admin["id"]), "yes", message_id)
        assert handled is True
        assert proposals.get(org_id, str(proposal["id"]))["status"] == "approved"

    def test_approve_words_are_case_and_whitespace_insensitive(self, org_id, admin):
        proposal = _pending_proposal(org_id, str(admin["id"]))
        message_id = _notified(org_id, proposal)
        handled = commands.dispatch_command(org_id, str(admin["id"]), "  Approve  ", message_id)
        assert handled is True
        assert proposals.get(org_id, str(proposal["id"]))["status"] == "approved"


class TestDecline:
    def test_no_quoting_the_notification_declines_it(self, org_id, admin):
        proposal = _pending_proposal(org_id, str(admin["id"]))
        message_id = _notified(org_id, proposal)
        handled = commands.dispatch_command(org_id, str(admin["id"]), "no", message_id)
        assert handled is True
        assert proposals.get(org_id, str(proposal["id"]))["status"] == "declined"


class TestUnrecognizedText:
    def test_ordinary_conversation_is_not_a_command(self, org_id, admin):
        proposal = _pending_proposal(org_id, str(admin["id"]))
        message_id = _notified(org_id, proposal)
        handled = commands.dispatch_command(
            org_id, str(admin["id"]), "sounds good, see you then!", message_id
        )
        assert handled is False
        assert proposals.get(org_id, str(proposal["id"]))["status"] == "pending"


class TestQuoteDoesNotResolveStopsCold:
    """The hard invariant JA's own incident established: a quote that
    doesn't resolve to anything actionable must NOT fall back to
    guessing at some other pending item."""

    def test_a_quote_that_matches_no_notification_is_not_a_command(self, org_id, admin):
        # A real pending proposal exists, but the quote doesn't name it.
        _pending_proposal(org_id, str(admin["id"]))
        handled = commands.dispatch_command(org_id, str(admin["id"]), "yes", "not-a-real-message-id")
        assert handled is False

    def test_no_quote_at_all_is_not_a_command_even_with_one_pending(self, org_id, admin):
        """No most-recent-pending fallback exists here at all -- an
        unquoted 'yes' is never a command, regardless of how many
        proposals are pending."""
        _pending_proposal(org_id, str(admin["id"]))
        handled = commands.dispatch_command(org_id, str(admin["id"]), "yes", None)
        assert handled is False

    def test_a_quote_belonging_to_a_different_users_proposal_is_not_a_command(
        self, org_id, admin, member
    ):
        proposal = _pending_proposal(org_id, str(member["id"]))
        message_id = _notified(org_id, proposal)
        handled = commands.dispatch_command(org_id, str(admin["id"]), "yes", message_id)
        assert handled is False
        assert proposals.get(org_id, str(proposal["id"]))["status"] == "pending"

    def test_a_quote_to_an_already_decided_proposal_is_not_a_command(self, org_id, admin):
        proposal = _pending_proposal(org_id, str(admin["id"]))
        message_id = _notified(org_id, proposal)
        proposals.approve(org_id, str(proposal["id"]))
        handled = commands.dispatch_command(org_id, str(admin["id"]), "no", message_id)
        assert handled is False
        assert proposals.get(org_id, str(proposal["id"]))["status"] == "approved"  # unchanged


class TestNoLLMInThisPath:
    def test_dispatch_command_never_reaches_an_llm_call(self, org_id, admin):
        proposal = _pending_proposal(org_id, str(admin["id"]))
        message_id = _notified(org_id, proposal)
        with mock.patch.object(ollama_llm, "call_structured") as fake_llm:
            commands.dispatch_command(org_id, str(admin["id"]), "yes", message_id)
            commands.dispatch_command(org_id, str(admin["id"]), "some free text", None)
        fake_llm.assert_not_called()

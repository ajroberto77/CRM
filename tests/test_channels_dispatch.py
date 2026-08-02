"""server/channels/dispatch.py -- R3's messaging axis. Both provider
adapters are mocked at their own module boundary throughout, same
convention as tests/test_calendar_provider.py.
"""
from __future__ import annotations

from unittest import mock

import pytest

from server.channels import dispatch, signal_cli, telegram
from server.core import user_channels, users


class TestSend:
    def test_disabled_by_default_raises(self, org_id, admin):
        user_channels.link_channel(org_id, str(admin["id"]), "signal", "+15550100123")
        with pytest.raises(dispatch.MessagingDisabledError):
            dispatch.send(org_id, str(admin["id"]), "hello")

    def test_no_linked_channel_raises(self, org_id, admin, monkeypatch):
        monkeypatch.setattr(
            "server.channels.dispatch.config.messaging_send_enabled", lambda: True
        )
        with pytest.raises(dispatch.NoLinkedChannelError):
            dispatch.send(org_id, str(admin["id"]), "hello")

    def test_dispatches_to_the_linked_providers_own_adapter(self, org_id, admin, monkeypatch):
        monkeypatch.setattr(
            "server.channels.dispatch.config.messaging_send_enabled", lambda: True
        )
        user_channels.link_channel(org_id, str(admin["id"]), "signal", "+15550100123")
        with mock.patch.object(signal_cli, "send", return_value="msg-1") as fake_send:
            message_id = dispatch.send(org_id, str(admin["id"]), "hello", thread_to="prior")
        assert message_id == "msg-1"
        fake_send.assert_called_once_with("+15550100123", "hello", thread_to="prior")

    def test_dispatches_to_telegram_when_thats_the_linked_provider(
        self, org_id, admin, monkeypatch
    ):
        monkeypatch.setattr(
            "server.channels.dispatch.config.messaging_send_enabled", lambda: True
        )
        user_channels.link_channel(org_id, str(admin["id"]), "telegram", "@somehandle")
        with mock.patch.object(telegram, "send", return_value="msg-2") as fake_send:
            message_id = dispatch.send(org_id, str(admin["id"]), "hello")
        assert message_id == "msg-2"
        fake_send.assert_called_once_with("somehandle", "hello", thread_to=None)


class TestReceiveCommands:
    def test_resolves_a_known_sender_to_their_org_and_user_id(self, org_id, admin):
        user_channels.link_channel(org_id, str(admin["id"]), "signal", "+15550100123")
        with mock.patch.object(
            signal_cli, "receive_commands", return_value=[("+15550100123", "yes", None)],
        ), mock.patch.object(telegram, "receive_commands", return_value=[]):
            commands = dispatch.receive_commands()
        assert commands == [(org_id, str(admin["id"]), "yes", None)]

    def test_an_unrecognized_sender_is_dropped(self, org_id):
        with mock.patch.object(
            signal_cli, "receive_commands", return_value=[("+19995550000", "yes", None)],
        ), mock.patch.object(telegram, "receive_commands", return_value=[]):
            commands = dispatch.receive_commands()
        assert commands == []

    def test_one_providers_failure_does_not_stop_the_others_poll(self, org_id, admin):
        user_channels.link_channel(org_id, str(admin["id"]), "telegram", "@somehandle")
        with mock.patch.object(
            signal_cli, "receive_commands", side_effect=RuntimeError("signal-cli not installed"),
        ), mock.patch.object(
            telegram, "receive_commands", return_value=[("somehandle", "no", "7")],
        ):
            commands = dispatch.receive_commands()
        assert commands == [(org_id, str(admin["id"]), "no", "7")]

    def test_commands_from_both_providers_are_combined(self, org_id, admin, member):
        user_channels.link_channel(org_id, str(admin["id"]), "signal", "+15550100123")
        user_channels.link_channel(org_id, str(member["id"]), "telegram", "@member")
        with mock.patch.object(
            signal_cli, "receive_commands", return_value=[("+15550100123", "yes", None)],
        ), mock.patch.object(
            telegram, "receive_commands", return_value=[("member", "no", None)],
        ):
            commands = dispatch.receive_commands()
        assert (org_id, str(admin["id"]), "yes", None) in commands
        assert (org_id, str(member["id"]), "no", None) in commands

    def test_a_sender_is_resolved_against_every_org_not_just_one(self, org_id, admin):
        """The regression this guards: `receive_commands()` used to take an
        `org_id` and be called once per org by `message_poller.py`, which
        drained each provider's non-replayable queue on the first org's
        turn and silently lost every other org's commands. It now polls
        each provider once and resolves the sender against every org --
        exercised here with the sender linked in a SECOND org, not the
        first one `all_org_ids()` would try."""
        second_org = users.create_org("Second Org", "second-dispatch-org")
        second_org_id = str(second_org["id"])
        second_admin = users.create_user(
            second_org_id, email="admin2@example.com", name="Admin Two",
            password="correct-horse-battery", is_admin=True,
        )
        user_channels.link_channel(
            second_org_id, str(second_admin["id"]), "signal", "+15550199999"
        )
        with mock.patch.object(
            signal_cli, "receive_commands", return_value=[("+15550199999", "yes", None)],
        ), mock.patch.object(telegram, "receive_commands", return_value=[]):
            commands = dispatch.receive_commands()
        assert commands == [(second_org_id, str(second_admin["id"]), "yes", None)]


class TestUnknownProvider:
    def test_an_unknown_channel_kind_raises_at_send_time(self, org_id, admin, monkeypatch):
        monkeypatch.setattr(
            "server.channels.dispatch.config.messaging_send_enabled", lambda: True
        )
        with mock.patch.object(
            user_channels, "list_channels",
            return_value=[{"kind": "carrier_pigeon", "value_normalized": "loft-1"}],
        ):
            with pytest.raises(dispatch.ChannelError):
                dispatch.send(org_id, str(admin["id"]), "hello")

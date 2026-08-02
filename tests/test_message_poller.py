"""server/jobs/message_poller.py -- dispatch.receive_commands() and
commands.dispatch_command() are mocked at their own boundaries; this only
exercises the poller's own per-org, per-command loop and its
one-bad-command-must-not-drop-the-rest-of-the-batch behavior.
"""
from __future__ import annotations

from unittest import mock

from server.jobs import message_poller
from server.channels import commands, dispatch


class TestPollOnce:
    def test_dispatches_every_command_from_every_org(self, org_id, admin):
        with mock.patch.object(
            dispatch, "receive_commands", return_value=[(str(admin["id"]), "yes", "msg-1")],
        ), mock.patch.object(commands, "dispatch_command") as fake_dispatch:
            message_poller.poll_once()
        fake_dispatch.assert_called_once_with(org_id, str(admin["id"]), "yes", "msg-1")

    def test_one_bad_command_does_not_stop_the_rest_of_the_batch(self, org_id, admin, member):
        calls = [
            (str(admin["id"]), "yes", "msg-1"),
            (str(member["id"]), "no", "msg-2"),
        ]
        with mock.patch.object(dispatch, "receive_commands", return_value=calls), \
             mock.patch.object(
                 commands, "dispatch_command",
                 side_effect=[RuntimeError("boom"), True],
             ) as fake_dispatch:
            message_poller.poll_once()
        assert fake_dispatch.call_count == 2

    def test_no_commands_is_a_no_op(self, org_id):
        with mock.patch.object(dispatch, "receive_commands", return_value=[]), \
             mock.patch.object(commands, "dispatch_command") as fake_dispatch:
            message_poller.poll_once()
        fake_dispatch.assert_not_called()

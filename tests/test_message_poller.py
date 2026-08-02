"""server/jobs/message_poller.py -- dispatch.receive_commands() and
commands.dispatch_command() are mocked at their own boundaries; this only
exercises the poller's own per-command loop (org_id now comes back from
receive_commands() itself, resolved per command, not looped by the
poller) and its one-bad-command-must-not-drop-the-rest-of-the-batch
behavior.
"""
from __future__ import annotations

from unittest import mock

from server.jobs import message_poller
from server.channels import commands, dispatch


class TestPollOnce:
    def test_dispatches_every_command_from_every_org(self, org_id, admin):
        with mock.patch.object(
            dispatch, "receive_commands",
            return_value=[(org_id, str(admin["id"]), "yes", "msg-1")],
        ), mock.patch.object(commands, "dispatch_command") as fake_dispatch:
            message_poller.poll_once()
        fake_dispatch.assert_called_once_with(org_id, str(admin["id"]), "yes", "msg-1")

    def test_one_bad_command_does_not_stop_the_rest_of_the_batch(self, org_id, admin, member):
        calls = [
            (org_id, str(admin["id"]), "yes", "msg-1"),
            (org_id, str(member["id"]), "no", "msg-2"),
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

    def test_receive_commands_is_called_once_not_per_org(self, org_id):
        """The regression this guards: an earlier version looped
        `for org_id in jobs.all_org_ids()` around `dispatch.receive_commands(org_id)`,
        which -- because signal-cli/Telegram polling is deployment-wide,
        not per-org -- drained each provider's non-replayable queue on the
        first org's turn and silently lost every other org's commands
        every cycle. `receive_commands()` now takes no `org_id` and is
        called exactly once per poll."""
        with mock.patch.object(
            dispatch, "receive_commands", return_value=[]
        ) as fake_receive, mock.patch.object(commands, "dispatch_command"):
            message_poller.poll_once()
        fake_receive.assert_called_once_with()

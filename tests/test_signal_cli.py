"""server/channels/signal_cli.py -- subprocess.run is mocked throughout,
no real signal-cli binary anywhere in the test suite.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from server.channels import signal_cli


def _proc(*, stdout="", stderr="", returncode=0):
    return mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)


class TestSend:
    def test_sends_via_signal_cli_and_returns_the_message_id(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_CLI_NUMBER", "+15550100000")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _proc(stdout="1700000000000\n")

        with mock.patch.object(signal_cli.subprocess, "run", side_effect=fake_run):
            message_id = signal_cli.send("+15550100123", "hello")
        assert message_id == "1700000000000"
        assert captured["cmd"][:3] == ["signal-cli", "-u", "+15550100000"]
        assert "send" in captured["cmd"]
        assert "+15550100123" in captured["cmd"]

    def test_thread_to_adds_quote_flags(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_CLI_NUMBER", "+15550100000")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _proc(stdout="1700000000001\n")

        with mock.patch.object(signal_cli.subprocess, "run", side_effect=fake_run):
            signal_cli.send("+15550100123", "hello", thread_to="1700000000000")
        assert "--quote-timestamp" in captured["cmd"]
        assert "1700000000000" in captured["cmd"]
        assert "--quote-author" in captured["cmd"]
        assert "+15550100000" in captured["cmd"]

    def test_missing_bot_number_raises(self, monkeypatch):
        monkeypatch.delenv("SIGNAL_CLI_NUMBER", raising=False)
        with pytest.raises(signal_cli.SignalError):
            signal_cli.send("+15550100123", "hello")

    def test_a_failed_send_raises(self, monkeypatch):
        import subprocess as real_subprocess
        monkeypatch.setenv("SIGNAL_CLI_NUMBER", "+15550100000")
        with mock.patch.object(
            signal_cli.subprocess, "run",
            side_effect=real_subprocess.CalledProcessError(1, ["signal-cli"]),
        ):
            with pytest.raises(signal_cli.SignalError):
                signal_cli.send("+15550100123", "hello")


class TestReceiveCommands:
    def test_parses_a_text_message_envelope(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_CLI_NUMBER", "+15550100000")
        envelope = {
            "envelope": {
                "sourceNumber": "+15550100123",
                "dataMessage": {"message": "yes"},
            }
        }
        stdout = json.dumps(envelope) + "\n"
        with mock.patch.object(signal_cli.subprocess, "run", return_value=_proc(stdout=stdout)):
            commands = signal_cli.receive_commands()
        assert commands == [("+15550100123", "yes", None)]

    def test_a_quoted_reply_carries_the_quoted_id(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_CLI_NUMBER", "+15550100000")
        envelope = {
            "envelope": {
                "sourceNumber": "+15550100123",
                "dataMessage": {"message": "yes", "quote": {"id": 1700000000000}},
            }
        }
        stdout = json.dumps(envelope) + "\n"
        with mock.patch.object(signal_cli.subprocess, "run", return_value=_proc(stdout=stdout)):
            commands = signal_cli.receive_commands()
        assert commands == [("+15550100123", "yes", "1700000000000")]

    def test_a_receipt_envelope_with_no_data_message_is_skipped(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_CLI_NUMBER", "+15550100000")
        envelope = {"envelope": {"sourceNumber": "+15550100123", "receiptMessage": {}}}
        stdout = json.dumps(envelope) + "\n"
        with mock.patch.object(signal_cli.subprocess, "run", return_value=_proc(stdout=stdout)):
            commands = signal_cli.receive_commands()
        assert commands == []

    def test_a_non_json_status_line_is_skipped(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_CLI_NUMBER", "+15550100000")
        with mock.patch.object(
            signal_cli.subprocess, "run",
            return_value=_proc(stdout="not json at all\n"),
        ):
            assert signal_cli.receive_commands() == []

    def test_a_nonzero_exit_returns_no_commands(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_CLI_NUMBER", "+15550100000")
        with mock.patch.object(
            signal_cli.subprocess, "run",
            return_value=_proc(returncode=1, stderr="boom"),
        ):
            assert signal_cli.receive_commands() == []

    def test_missing_bot_number_raises(self, monkeypatch):
        monkeypatch.delenv("SIGNAL_CLI_NUMBER", raising=False)
        with pytest.raises(signal_cli.SignalError):
            signal_cli.receive_commands()

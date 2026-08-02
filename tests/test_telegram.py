"""server/channels/telegram.py -- urllib.request.urlopen is mocked
throughout (same convention as tests/test_microsoft_graph.py), no real
network call anywhere.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from server.channels import telegram


def _fake_response(body: dict):
    return mock.Mock(
        __enter__=lambda s: s, __exit__=lambda *a: None,
        read=lambda: json.dumps(body).encode(),
    )


@pytest.fixture(autouse=True)
def _reset_offset():
    telegram._last_update_id = None
    yield
    telegram._last_update_id = None


class TestSend:
    def test_sends_and_returns_the_message_id(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return _fake_response({"ok": True, "result": {"message_id": 42}})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            message_id = telegram.send("123456", "hello")
        assert message_id == "42"
        assert "test-token" in captured["url"]
        assert captured["body"]["chat_id"] == "123456"
        assert captured["body"]["text"] == "hello"

    def test_thread_to_sets_reply_to_message_id(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _fake_response({"ok": True, "result": {"message_id": 43}})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            telegram.send("123456", "hello", thread_to="41")
        assert captured["body"]["reply_to_message_id"] == 41

    def test_missing_bot_token_raises(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(telegram.TelegramError):
            telegram.send("123456", "hello")

    def test_an_api_level_failure_raises(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, timeout=None: _fake_response(
                {"ok": False, "description": "chat not found"}
            ),
        ):
            with pytest.raises(telegram.TelegramError):
                telegram.send("123456", "hello")


class TestReceiveCommands:
    def test_parses_a_text_message_update(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        updates = [{
            "update_id": 1001,
            "message": {"message_id": 5, "chat": {"id": 123456}, "text": "yes"},
        }]
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, timeout=None: _fake_response({"ok": True, "result": updates}),
        ):
            commands = telegram.receive_commands()
        assert commands == [("123456", "yes", None)]

    def test_a_reply_carries_the_quoted_message_id(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        updates = [{
            "update_id": 1002,
            "message": {
                "message_id": 6, "chat": {"id": 123456}, "text": "no",
                "reply_to_message": {"message_id": 5},
            },
        }]
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, timeout=None: _fake_response({"ok": True, "result": updates}),
        ):
            commands = telegram.receive_commands()
        assert commands == [("123456", "no", "5")]

    def test_the_offset_advances_past_seen_updates(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        captured = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data.decode())
            captured.append(body)
            return _fake_response({"ok": True, "result": []})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            telegram._last_update_id = 1001
            telegram.receive_commands()
        assert captured[0]["offset"] == 1002

    def test_a_non_text_update_is_skipped(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        updates = [{"update_id": 1003, "message": {"message_id": 7, "chat": {"id": 123456}}}]
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, timeout=None: _fake_response({"ok": True, "result": updates}),
        ):
            assert telegram.receive_commands() == []

    def test_a_transient_api_failure_returns_no_commands_not_an_error(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, timeout=None: _fake_response(
                {"ok": False, "description": "boom"}
            ),
        ):
            assert telegram.receive_commands() == []

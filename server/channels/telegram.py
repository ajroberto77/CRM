"""Telegram adapter for R3's messaging axis (`server/channels/dispatch.py`).
New work -- JA has no Telegram code. Bot API long-polling (`getUpdates`),
matching signal-cli's poll shape and avoiding a public webhook for a
first cut, using `config.py`'s already-present `telegram_bot_token`
secret.

`chat_id` is both the destination `send()` targets and the opaque sender
identity `receive_commands()` returns -- for a bot's private 1:1 chat
with a user (the only case this platform's approval flow needs), a
message's `chat.id` and its sender's `from.id` are the same number, so
`chat_id` alone is enough to both identify the sender and reply to them.
`message_id` (scoped to that chat) is the opaque id `send()` returns and
`reply_to_message_id` threads under.

## The update offset (a documented first-cut simplification)

Telegram's `getUpdates` never auto-advances: a client must pass
`offset = last_update_id + 1` on the NEXT call to acknowledge everything
up to and including `last_update_id`, or the same updates are returned
again. That offset is kept in-memory only (module-level, like
`signal_cli.py`'s rate-gated cleanup globals) -- a process restart
re-delivers whatever Telegram is still holding (bounded by its own ~24h
retention), which is safe rather than dangerous: a redelivered "yes"/"no"
against a proposal that's already been decided hits
`proposals.AlreadyDecidedError` in `commands.py` and is simply not acted
on again. Persisting the offset durably would need a piece of state this
platform has no natural per-org home for (the bot token, unlike a
connected account, is one per deployment, not one per org) -- left as a
known limitation, not a silent gap.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from server import config
from server.net import http_retry

_BASE = "https://api.telegram.org"
_last_update_id: Optional[int] = None


class TelegramError(RuntimeError):
    pass


def _bot_token() -> str:
    token = config.get_secret("telegram_bot_token")
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
    return token


def _call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{_BASE}/bot{_bot_token()}/{method}"
    try:
        response = http_retry.request_with_retry(
            "POST", url,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload).encode("utf-8"),
            max_retries=3,
        )
    except http_retry.HTTPRetryError as exc:
        raise TelegramError(f"{method} failed: {exc}") from exc
    if not response.get("ok"):
        raise TelegramError(f"{method} failed: {response.get('description')}")
    return response["result"]


def send(destination: str, text: str, *, thread_to: Optional[str] = None) -> str:
    """`destination` is the target chat id (a `user_channels` row's
    `value_normalized`). Returns the new message's id, scoped to that
    chat -- store it, never interpret it."""
    payload: dict[str, Any] = {"chat_id": destination, "text": text}
    if thread_to:
        payload["reply_to_message_id"] = int(thread_to)
    result = _call("sendMessage", payload)
    return str(result["message_id"])


def _extract_command(update: dict[str, Any]) -> Optional[tuple[str, str, Optional[str]]]:
    """(chat_id, text, quoted_id) from one Telegram update, or None for
    anything that isn't a plain text message (edits, non-text media,
    channel posts)."""
    message = update.get("message")
    if not message:
        return None
    text = message.get("text")
    if not text:
        return None
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return None
    reply = message.get("reply_to_message") or {}
    quoted_id = reply.get("message_id")
    return str(chat_id), text.strip(), (str(quoted_id) if quoted_id is not None else None)


def receive_commands() -> list[tuple[str, str, Optional[str]]]:
    """One long-poll of `getUpdates`, parsed down to (chat_id, text,
    quoted_id) for every plain text message received. Advances the
    in-memory offset past every update returned, whether or not it turned
    out to be a recognized command -- an update is acknowledged once
    seen, same as signal-cli's own receive semantics."""
    global _last_update_id
    payload: dict[str, Any] = {"timeout": 5}
    if _last_update_id is not None:
        payload["offset"] = _last_update_id + 1
    try:
        updates = _call("getUpdates", payload)
    except TelegramError:
        return []  # a transient Bot API failure must not crash the poller

    commands: list[tuple[str, str, Optional[str]]] = []
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            _last_update_id = update_id if _last_update_id is None else max(_last_update_id, update_id)
        extracted = _extract_command(update)
        if extracted:
            commands.append(extracted)
    return commands

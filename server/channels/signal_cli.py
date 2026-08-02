"""Signal adapter for R3's messaging axis (`server/channels/dispatch.py`).
Ported from JA's `messaging.py` -- the `signal-cli` invocation shape, the
global-flags-before-subcommand ordering, and the libsignal temp-dir
cleanup are carried over verbatim, since each is a real fix for a real
signal-cli behavior, not a design choice to reconsider.

One difference from JA: JA was single-owner (one bot number talking to
exactly one human, identified by `OPENCLAW_OWNER_NUMBER`), so its
`_signal_extract_command()` filtered to that one sender inline. This
platform is multi-user, so that filtering step is gone here --
`receive_commands()` returns every text message from every sender, and
`server/channels/dispatch.py` resolves each sender number to a CRM user
via `core.user_channels` (or drops it, unrecognized). Sender-identity
filtering happens exactly once, at that one resolution point, rather than
duplicated inside each provider adapter.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from server import config

# signal-cli's bundled libsignal native library extracts itself into a
# fresh, randomly-named ~167MB temp directory on EVERY invocation and does
# not clean up after itself (observed: signal-cli 0.14.5). Under enough
# invocations -- a burst of notifications, or just weeks of a long-running
# poller -- these accumulate and can exhaust /tmp's quota, at which point
# EVERY subsequent signal-cli call fails with "Disk quota exceeded",
# unrelated to whatever specifically tipped it over. Cleanup lives here,
# not as a host-level systemd-tmpfiles config, so it travels with this
# codebase wherever it runs -- no separate ops step to remember.
_SIGNAL_TMP_CLEANUP_ENABLED = (
    os.environ.get("SIGNAL_CLI_TMP_CLEANUP_ENABLED", "true").lower() == "true"
)
_SIGNAL_TMP_CLEANUP_INTERVAL = float(
    os.environ.get("SIGNAL_CLI_TMP_CLEANUP_INTERVAL_SECONDS", "3600")
)
_SIGNAL_TMP_MAX_AGE = float(os.environ.get("SIGNAL_CLI_TMP_MAX_AGE_SECONDS", "3600"))
_last_signal_tmp_cleanup = 0.0


class SignalError(RuntimeError):
    pass


def _cleanup_stale_libsignal_tmp() -> None:
    """Remove old orphaned `libsignal*` temp dirs. Scoped to directories
    old enough that no concurrently-running signal-cli invocation could
    still be extracting into them (a fresh extraction is at most a few
    seconds old; the default 1h threshold has enormous margin). Never
    fatal -- a cleanup failure here must never take down an actual
    send/receive."""
    tmp_dir = Path(tempfile.gettempdir())
    now = time.time()
    try:
        for path in tmp_dir.glob("libsignal*"):
            try:
                if not path.is_dir():
                    continue
                if now - path.stat().st_mtime < _SIGNAL_TMP_MAX_AGE:
                    continue  # too young -- could still be in use
                shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue  # one bad entry must never stop the rest
    except OSError:
        pass  # cleanup itself must never break a send/receive


def _maybe_cleanup_stale_libsignal_tmp() -> None:
    """Rate-gated: the real scan only runs once per
    SIGNAL_CLI_TMP_CLEANUP_INTERVAL_SECONDS regardless of how often
    send()/receive_commands() call this -- cheap to call on every one."""
    global _last_signal_tmp_cleanup
    if not _SIGNAL_TMP_CLEANUP_ENABLED:
        return
    now = time.time()
    if now - _last_signal_tmp_cleanup < _SIGNAL_TMP_CLEANUP_INTERVAL:
        return
    _last_signal_tmp_cleanup = now
    _cleanup_stale_libsignal_tmp()


def send(destination: str, text: str, *, thread_to: Optional[str] = None) -> str:
    """`destination` is the recipient's E.164 number (a `user_channels`
    row's `value_normalized`). `-u <bot number>` is a GLOBAL flag that
    must precede the subcommand. Returns signal-cli's own message
    timestamp (its message id) -- the only thing printed to stdout on
    success. Raises on failure: a failed send must be loud."""
    _maybe_cleanup_stale_libsignal_tmp()
    bin_path = os.environ.get("SIGNAL_CLI_BIN", "signal-cli")
    bot_number = config.get_secret("signal_cli_number")
    if not bot_number:
        raise SignalError("SIGNAL_CLI_NUMBER is not set")
    cmd = [bin_path, "-u", bot_number, "send", destination, "-m", text]
    if thread_to:
        # Thread by sending a Signal quote-reply to the earlier message.
        # quote-author is the bot's OWN number, since every notification
        # this platform sends was sent by the bot itself.
        cmd += ["--quote-timestamp", str(thread_to), "--quote-author", bot_number]
    try:
        proc = subprocess.run(cmd, check=True, timeout=60, capture_output=True, text=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise SignalError(f"signal-cli send failed: {exc}") from exc
    message_id = proc.stdout.strip()
    if not message_id:
        raise SignalError("signal-cli send produced no message id")
    return message_id


def _extract_command(envelope: dict) -> Optional[tuple[str, str, Optional[str]]]:
    """(sender_number, text, quoted_id) from one signal-cli JSON envelope,
    or None for anything that isn't a text message (receipts, typing
    indicators, group messages)."""
    env = envelope.get("envelope", envelope)  # some signal-cli versions nest one level, some don't
    source = env.get("sourceNumber") or env.get("source")
    if not source:
        return None
    data_message = env.get("dataMessage")
    if not data_message:
        return None
    text = data_message.get("message")
    if not text:
        return None
    quote = data_message.get("quote") or {}
    quoted_id = quote.get("id")
    return source, text.strip(), (str(quoted_id) if quoted_id is not None else None)


def receive_commands() -> list[tuple[str, str, Optional[str]]]:
    """One poll of `signal-cli -o json -u <number> receive -t 5`, parsed
    down to (sender_number, text, quoted_id) for every text message
    received. `-o`/`--output` is a GLOBAL signal-cli flag and must come
    BEFORE the subcommand (verified against signal-cli 0.14.5; `receive`
    has no `--json` of its own)."""
    _maybe_cleanup_stale_libsignal_tmp()
    bin_path = os.environ.get("SIGNAL_CLI_BIN", "signal-cli")
    bot_number = config.get_secret("signal_cli_number")
    if not bot_number:
        raise SignalError("SIGNAL_CLI_NUMBER is not set")
    config_dir = os.environ.get("SIGNAL_CLI_CONFIG_DIR", "")
    cmd = [bin_path]
    if config_dir:
        cmd += ["--config", config_dir]
    cmd += ["-o", "json", "-u", bot_number, "receive", "-t", "5"]
    proc = subprocess.run(cmd, check=False, timeout=30, capture_output=True, text=True)
    if proc.returncode != 0:
        print(
            f"[signal_cli] receive exited {proc.returncode}: {proc.stderr.strip()[:300]}",
            file=sys.stderr, flush=True,
        )
        return []
    commands: list[tuple[str, str, Optional[str]]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            continue  # signal-cli sometimes prints non-JSON status lines; skip them
        extracted = _extract_command(envelope)
        if extracted:
            commands.append(extracted)
    return commands

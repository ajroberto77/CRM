"""R3's messaging axis dispatcher.

Generic core, name-prefixed adapters (R2): this module carries no
provider name and never imports a provider module except through the
dispatch functions below, each with its own lazy import -- matching
`server/providers/esign.py`'s and `calendar.py`'s established shape in
this codebase. Adding a third provider (email, per R3's table) is one new
`<provider>.py` file implementing `send`/`receive_commands`, plus one
`elif` in each of the two functions here.

Contract ported from JA's `messaging.py` (`send`/`receive_commands`,
opaque message ids -- callers store one, never interpret it), generalized
for this platform's multi-user shape: JA's single owner-number filter is
gone, replaced by `core.user_channels` resolving each inbound sender to a
CRM user (`server/core/user_channels.py`) at this one choke point rather
than duplicated inside each adapter.
"""
from __future__ import annotations

from typing import Optional

from server import config
from server.core import user_channels

PROVIDERS = ("signal", "telegram")


class ChannelError(RuntimeError):
    pass


class MessagingDisabledError(ChannelError):
    """Raised by `send()` when `config.messaging_send_enabled()` is false
    -- the one real gate at this choke point (safety rule 9). Raises
    rather than no-ops (safety rule 3): a caller that doesn't explicitly
    handle this fails loudly instead of silently believing a message went
    out."""


class NoLinkedChannelError(ChannelError):
    """Raised by `send()` when `user_id` has no linked `user_channels`
    row for any known provider."""


def send(org_id: str, user_id: str, text: str, *, thread_to: Optional[str] = None) -> str:
    """Send `text` to `user_id` via whichever channel they've linked. If a
    user has linked more than one provider, the earliest-linked one wins
    (`user_channels.list_channels()`'s own `created_at` order) -- a user
    with two linked channels is an edge case a first cut resolves
    deterministically rather than guessing which one they'd prefer right
    now. Returns the new message's opaque id -- store it, never interpret
    it (JA's own convention, ported). `thread_to` (an id previously
    returned by `send()`) threads this message under that earlier one, if
    the provider supports it."""
    if not config.messaging_send_enabled():
        raise MessagingDisabledError(
            "messaging_send_enabled is false -- no channel send was attempted"
        )
    channels = user_channels.list_channels(org_id, user_id=user_id)
    if not channels:
        raise NoLinkedChannelError(f"user {user_id} has no linked messaging channel")
    channel = channels[0]

    provider = channel["kind"]
    if provider == "signal":
        import server.channels.signal_cli as impl
    elif provider == "telegram":
        import server.channels.telegram as impl
    else:
        raise ChannelError(
            f"unknown channel provider {provider!r} (known: {', '.join(PROVIDERS)})"
        )
    return impl.send(channel["value_normalized"], text, thread_to=thread_to)


def receive_commands() -> list[tuple[str, str, str, Optional[str]]]:
    """Poll every provider ONCE for inbound commands, resolving each
    sender to an `(org_id, user_id)` via `core.user_channels`. Returns
    `[(org_id, user_id, text, quoted_id), ...]` in arrival order across
    all providers; an unrecognized sender (no matching `user_channels` row
    in any org) is silently dropped -- not a command, matching JA's own
    "not the owner -> not a command" rule generalized to "not a known
    user."

    Deliberately takes no `org_id`: signal-cli's bot number and Telegram's
    bot token are both deployment-wide, not one per org (see
    `telegram.py`'s own docstring on `_last_update_id`), so each
    provider's `receive_commands()` must be called exactly once per poll
    cycle. Calling it once per org (an earlier version of this function
    did) drains each provider's non-replayable inbound queue on the FIRST
    org's call -- Telegram's offset advances, signal-cli's `receive`
    acknowledges -- so every other org's commands are silently and
    permanently lost every cycle, a real safety-rule-4 violation, not a
    theoretical one. Resolving a raw sender to the org it belongs to is
    what has to check every org (`user_channels.resolve_user_any_org()`);
    the provider poll itself never repeats.

    One provider's failure (unconfigured secret, a transient API error)
    must not stop the other's poll -- each adapter's `receive_commands()`
    is called independently and a failure here is swallowed, same
    "a transient error must never kill the poller" rule
    `server/jobs/sync_poller.py` already applies per-account.
    """
    commands: list[tuple[str, str, str, Optional[str]]] = []
    for provider in PROVIDERS:
        if provider == "signal":
            import server.channels.signal_cli as impl
        else:
            import server.channels.telegram as impl
        try:
            raw_commands = impl.receive_commands()
        except Exception:  # noqa: BLE001 -- one provider's failure must not stop the other's poll
            continue
        for raw_sender, text, quoted_id in raw_commands:
            resolved = user_channels.resolve_user_any_org(provider, raw_sender)
            if resolved is None:
                continue
            resolved_org_id, resolved_user_id = resolved
            commands.append((resolved_org_id, resolved_user_id, text, quoted_id))
    return commands

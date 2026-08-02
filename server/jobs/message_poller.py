"""M6's Signal/Telegram command loop -- a second standalone sleep loop,
same shape as `sync_poller.py` (Cal's `poll_loop.py` pattern), since M7's
general work queue doesn't exist yet.

`config.messaging_send_enabled()` gates `dispatch.send()` itself (the
real choke point, safety rule 9) -- this poller only ever calls
`dispatch.receive_commands()`, never `send()` directly, so there is
nothing of its own to gate here: receiving and acting on an inbound
command is not a destructive default-off path, only a real outbound
Signal/Telegram message is.

`dispatch.receive_commands()` is called exactly ONCE per cycle, with no
per-org loop here: it polls each provider's deployment-wide inbound queue
a single time and resolves every returned sender to its own org
internally (see its own docstring) -- looping org_ids around the call, the
way `sync_poller.py`'s per-account loop does for connected accounts, would
drain each provider's non-replayable queue on the first org's turn and
silently lose every other org's commands.

Each command is processed in its own try/except: a command returned by
`receive_commands()` has already been consumed at the provider (signal-
cli's `receive`/Telegram's `getUpdates` offset both acknowledge on
fetch), so it cannot be re-fetched on a retry the way a sync delta cursor
can. One bad command failing must not cost the rest of the same poll's
batch -- the same "a transient failure must not silently drop data"
concern safety rule 4 names for sync, applied here to a different kind of
non-replayable fetch.

Run as its own process: `python3 -m server.jobs.message_poller`.
"""
from __future__ import annotations

import time

from server.channels import commands, dispatch

_DEFAULT_INTERVAL_SECONDS = 15


def poll_once() -> None:
    for org_id, user_id, text, quoted_id in dispatch.receive_commands():
        try:
            commands.dispatch_command(org_id, user_id, text, quoted_id)
        except Exception as exc:  # noqa: BLE001 -- see module docstring: this command is
            # already consumed and cannot be retried, so one failure must not stop the
            # rest of this batch from being processed.
            print(
                f"[message_poller] ERROR processing a command from user {user_id} "
                f"(org {org_id}): {exc}"
            )


def run_forever(interval_seconds: int = _DEFAULT_INTERVAL_SECONDS) -> None:
    while True:
        try:
            poll_once()
        except Exception as exc:  # noqa: BLE001 -- a transient error must never kill the daemon
            print(f"[message_poller] ERROR: {exc}")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    run_forever()

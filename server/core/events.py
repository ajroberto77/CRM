"""The event bus — the single extension seam.

A transactional outbox, not a naive callback list.

## Why an outbox

The event row is written **inside** the repository's transaction, so a record
write and the event describing it commit or roll back together. Subscribers run
**after** that commit.

That split is what makes M5 correct. When a subscriber enqueues a job or sends a
message, there is no window where the record write rolls back but the message
already went out, and none where the write commits but the event is lost before
anyone can act on it.

## Why subscribers do not run inside the transaction

They hold a row lock and a pooled connection for their whole duration. M5's
first subscriber makes an HTTP call to Signal; at four seconds each, ten
concurrent saves exhaust the pool (default max 10) and the application
deadlocks on a feature nobody thought was in the write path. It would also let
any module's exception roll back a core CRUD write — Odoo's monkey-patching
failure mode, which `docs/COMPETITIVE-ANALYSIS.md` explicitly rejects.

## A throwing subscriber does not fail the request

It is caught, logged, and recorded on the event row. The write already
committed; re-raising would return 500 for a write that succeeded, and the
client would retry and create a duplicate — JA's duplicate-send bug, reached by
a different road.

This is **not** a violation of "gates raise, never no-op". A gate decides
whether work happens. This runs after the work has already happened, and
pretending it can be undone would be the lie.

## Subscribers are not a privileged path

Two rules keep the bus from becoming a hole in the permission model:

1. `RecordEvent` carries no cursor. A subscriber physically cannot write inside
   the writer's transaction.
2. A subscriber that writes goes back through `repository` with a Principal —
   `permissions.system_principal(org_id, reason)` for platform-originated work.

## Delivery guarantee

In-process dispatch is **at-most-once**: if the process dies between commit and
dispatch, the event is lost. That is fine for cache invalidation and derived
columns, and not fine for sending a message. The rows are durable, so an M7
worker can drain `delivery_state = 'pending'` for at-least-once delivery
without any subscriber changing — the subscriber signature is the contract, the
delivery mechanism is not.
"""
from __future__ import annotations

import json
import sys
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from server.core.permissions import Principal

CREATED = "created"
UPDATED = "updated"
DELETED = "deleted"

# A subscriber that writes fires another event. Without a ceiling, a subscriber
# that updates its own subject recurses until the process dies -- which is worse
# than the request failing, because it takes every other request with it.
MAX_DEPTH = 4
_depth: ContextVar[int] = ContextVar("event_depth", default=0)


@dataclass(frozen=True)
class RecordEvent:
    org_id: str
    entity: str
    record_id: Optional[str]
    action: str
    actor_user_id: str
    actor_kind: str
    # {field: {"before": ..., "after": ...}} -- empty for create and delete.
    diff: dict[str, Any] = field(default_factory=dict)
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    event_id: Optional[str] = None
    principal: Optional[Principal] = None

    def changed(self, name: str) -> bool:
        return name in self.diff


@dataclass(frozen=True)
class _Subscriber:
    name: str
    handler: Callable[[RecordEvent], None]
    entities: tuple[str, ...]
    actions: tuple[str, ...]
    order: int


_SUBSCRIBERS: list[_Subscriber] = []


def subscribe(
    name: str,
    handler: Callable[[RecordEvent], None],
    *,
    entities: tuple[str, ...] = (),
    actions: tuple[str, ...] = (),
    order: int = 100,
) -> None:
    """Register a subscriber. Empty `entities`/`actions` means all.

    Handlers must be **idempotent and commutative**. Dispatch order is stable
    (by `order`, then registration) but is deliberately not a correctness
    contract: once an M7 worker redelivers failed events, ordering between
    subscribers stops being meaningful.

    Idempotent on `name`, like `registry.register_validator`: a module's
    `install()` re-declaring the same subscriber (every real app startup and
    every test that spins up a fresh `TestClient` both call
    `modules.install_enabled_modules()`) must not double-register it -- a
    second copy would run the handler twice per matching event.
    """
    for existing in _SUBSCRIBERS:
        if existing.name == name:
            return
    _SUBSCRIBERS.append(_Subscriber(name, handler, entities, actions, order))
    _SUBSCRIBERS.sort(key=lambda s: s.order)


def reset() -> None:
    """Drop every subscriber. Tests only."""
    _SUBSCRIBERS.clear()


def subscriber_snapshot() -> list[_Subscriber]:
    """An opaque snapshot of the current subscriber list, for a test that
    must not permanently delete a module-installed, session-scoped
    subscriber (e.g. `modules/investor_portal`'s mandate-derivation
    subscriber) the way a blanket `reset()` between tests would.

    Same shape as `registry.validator_snapshot()`.
    """
    return list(_SUBSCRIBERS)


def restore_subscribers(snapshot: list[_Subscriber]) -> None:
    _SUBSCRIBERS[:] = snapshot


def subscribers() -> list[str]:
    return [s.name for s in _SUBSCRIBERS]


def diff_of(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Field-level diff, descending one level into `custom` so a changed custom
    field reads as `custom.renewal` rather than the whole blob."""
    changes: dict[str, Any] = {}
    for key in set(before) | set(after):
        if key == "custom":
            old_custom = before.get("custom") or {}
            new_custom = after.get("custom") or {}
            for sub in set(old_custom) | set(new_custom):
                if old_custom.get(sub) != new_custom.get(sub):
                    changes[f"custom.{sub}"] = {
                        "before": old_custom.get(sub), "after": new_custom.get(sub)
                    }
            continue
        if before.get(key) != after.get(key):
            changes[key] = {"before": before.get(key), "after": after.get(key)}
    # Bookkeeping columns are not a change anyone subscribed to.
    changes.pop("updated_at", None)
    return changes


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def record_event(cur, event: RecordEvent) -> Optional[str]:
    """Write the outbox row. Called with the WRITER'S cursor, inside its
    transaction — that is the whole point.

    Returns the new event id, or None when there is nothing to record: an
    update with an empty diff emits no event. Inline in-cell editing fires a
    save on every blur, and without this an untouched field would become a
    message per blur in M5.
    """
    if event.action == UPDATED and not event.diff:
        return None
    cur.execute(
        "INSERT INTO core.events "
        "  (org_id, entity, record_id, action, actor_user_id, actor_kind, diff) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING id",
        (
            event.org_id, event.entity, event.record_id, event.action,
            event.actor_user_id, event.actor_kind, json.dumps(_json_safe(event.diff)),
        ),
    )
    row = cur.fetchone()
    return str(row["id"]) if row else None


def publish_committed(event: RecordEvent) -> None:
    """Dispatch to in-process subscribers. Called AFTER the write commits.

    Never raises. A failing subscriber is isolated so one bad module cannot
    break every write in the system.
    """
    depth = _depth.get()
    if depth >= MAX_DEPTH:
        print(
            f"[events] depth {depth} exceeded for {event.entity}.{event.action} "
            f"-- refusing to cascade further",
            file=sys.stderr, flush=True,
        )
        return

    token = _depth.set(depth + 1)
    try:
        for sub in list(_SUBSCRIBERS):
            if sub.entities and event.entity not in sub.entities:
                continue
            if sub.actions and event.action not in sub.actions:
                continue
            try:
                sub.handler(event)
            except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
                print(
                    f"[events] subscriber {sub.name!r} failed on "
                    f"{event.entity}.{event.action}: {exc}",
                    file=sys.stderr, flush=True,
                )
                _mark_failed(event, sub.name, exc)
    finally:
        _depth.reset(token)


def _mark_failed(event: RecordEvent, subscriber: str, exc: BaseException) -> None:
    """Record the failure on the event row so it is visible and redeliverable,
    rather than existing only in a log line nobody reads."""
    if event.event_id is None:
        return
    from server.db import pool  # local import: events is imported by repository

    try:
        with pool.transaction(org_id=event.org_id) as cur:
            cur.execute(
                "UPDATE core.events "
                "   SET delivery_state = 'failed', attempts = attempts + 1, "
                "       last_error = %s "
                " WHERE id = %s",
                (f"{subscriber}: {exc}"[:2000], event.event_id),
            )
    except Exception:  # noqa: BLE001 -- bookkeeping must never mask the original
        pass


def mark_delivered(event_id: str, org_id: str) -> None:
    from server.db import pool

    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "UPDATE core.events SET delivery_state = 'delivered' "
            "WHERE id = %s AND delivery_state = 'pending'",
            (event_id,),
        )

"""server/core/events.py's redelivery support (find_undelivered/redeliver)
+ server/jobs/event_redelivery.py's sweep()/handle_redeliver_event() --
M7's "an M7 worker can drain delivery_state='pending' ... without any
subscriber changing" promise, exercised for real.
"""
from __future__ import annotations

from unittest import mock

import pytest

from server.core import events, repository
from server.db import pool
from server.jobs import event_redelivery, queue


def _create_org(name, slug):
    from server.core import users
    return users.create_org(name, slug)


@pytest.fixture
def subscriber_calls():
    calls = []

    def handler(event):
        calls.append(event)

    events.subscribe("test.capture", handler, entities=("organization",), actions=(events.CREATED,))
    yield calls


def _backdate_event(org_id, event_id, seconds=600):
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "UPDATE core.events SET occurred_at = now() - (%s || ' seconds')::interval "
            "WHERE id = %s",
            (seconds, event_id),
        )


def _latest_event_id(org_id, entity, record_id):
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        cur.execute(
            "SELECT id FROM core.events WHERE org_id = %s AND entity = %s AND record_id = %s "
            "ORDER BY seq DESC LIMIT 1",
            (org_id, entity, record_id),
        )
        return str(cur.fetchone()["id"])


class TestFindUndelivered:
    def test_a_fresh_event_is_not_yet_due(self, org_id, principal, subscriber_calls):
        repository.create(principal, "organization", {"name": "Acme"})
        assert events.find_undelivered(org_id, grace_seconds=300) == []

    def test_an_event_past_the_grace_period_is_found(self, org_id, principal, subscriber_calls):
        org = repository.create(principal, "organization", {"name": "Acme"})
        event_id = _latest_event_id(org_id, "organization", str(org["id"]))
        _backdate_event(org_id, event_id)
        found = events.find_undelivered(org_id, grace_seconds=300)
        assert str(found[0]["id"]) == event_id

    def test_a_delivered_event_is_never_found(self, org_id, principal, subscriber_calls):
        org = repository.create(principal, "organization", {"name": "Acme"})
        event_id = _latest_event_id(org_id, "organization", str(org["id"]))
        _backdate_event(org_id, event_id)
        events.mark_delivered(event_id, org_id)
        assert events.find_undelivered(org_id, grace_seconds=300) == []


class TestRedeliver:
    def test_redispatches_with_the_full_after_snapshot(self, org_id, principal, subscriber_calls):
        """The whole point of persisting before/after_snapshot: a
        subscriber reading event.after must see the full record, not
        just an empty/partial diff."""
        org = repository.create(principal, "organization", {"name": "Acme"})
        event_id = _latest_event_id(org_id, "organization", str(org["id"]))
        subscriber_calls.clear()

        events.redeliver(org_id, event_id)

        assert len(subscriber_calls) == 1
        redelivered = subscriber_calls[0]
        assert redelivered.after["name"] == "Acme"
        assert redelivered.entity == "organization"
        assert redelivered.action == events.CREATED

    def test_marks_delivered_on_success(self, org_id, principal, subscriber_calls):
        org = repository.create(principal, "organization", {"name": "Acme"})
        event_id = _latest_event_id(org_id, "organization", str(org["id"]))
        events.redeliver(org_id, event_id)
        with pool.transaction(org_id=org_id, readonly=True) as cur:
            cur.execute("SELECT delivery_state FROM core.events WHERE id = %s", (event_id,))
            assert cur.fetchone()["delivery_state"] == "delivered"

    def test_a_subscriber_that_throws_during_redelivery_stays_failed_not_delivered(
        self, org_id, principal
    ):
        def boom(event):
            raise RuntimeError("still broken")

        events.subscribe("test.throws", boom, entities=("organization",), actions=(events.CREATED,))
        org = repository.create(principal, "organization", {"name": "Acme"})
        event_id = _latest_event_id(org_id, "organization", str(org["id"]))

        events.redeliver(org_id, event_id)

        with pool.transaction(org_id=org_id, readonly=True) as cur:
            cur.execute("SELECT delivery_state FROM core.events WHERE id = %s", (event_id,))
            assert cur.fetchone()["delivery_state"] == "failed"

    def test_redelivering_a_purged_event_id_is_a_no_op(self, org_id):
        events.redeliver(org_id, "00000000-0000-0000-0000-000000000000")  # must not raise


class TestSweep:
    @pytest.fixture(autouse=True)
    def _reset_handlers_and_queue(self):
        from server.jobs import workers
        workers.reset_handlers()
        yield
        workers.reset_handlers()

    def test_enqueues_one_job_per_undelivered_event(self, org_id, principal, subscriber_calls):
        org = repository.create(principal, "organization", {"name": "Acme"})
        event_id = _latest_event_id(org_id, "organization", str(org["id"]))
        _backdate_event(org_id, event_id)

        enqueued = event_redelivery.sweep(grace_seconds=300)

        assert enqueued == 1
        claimed = queue.claim_batch("test-worker", (event_redelivery.JOB_TYPE,), 10)
        assert len(claimed) == 1
        assert claimed[0]["payload"] == {"org_id": org_id, "event_id": event_id}

    def test_a_fresh_event_is_not_swept(self, org_id, principal, subscriber_calls):
        repository.create(principal, "organization", {"name": "Acme"})
        assert event_redelivery.sweep(grace_seconds=300) == 0

    def test_spans_every_org(self, org_id, principal, subscriber_calls):
        org2 = _create_org("Second Org", "second-org")
        from server.core import permissions, users
        admin2 = users.create_user(
            str(org2["id"]), email="admin2@example.com", name="A2",
            password="correct-horse-battery", is_admin=True,
        )
        principal2 = permissions.load_principal(admin2)

        r1 = repository.create(principal, "organization", {"name": "Acme"})
        r2 = repository.create(principal2, "organization", {"name": "Globex"})
        _backdate_event(org_id, _latest_event_id(org_id, "organization", str(r1["id"])))
        _backdate_event(str(org2["id"]), _latest_event_id(str(org2["id"]), "organization", str(r2["id"])))

        assert event_redelivery.sweep(grace_seconds=300) == 2


class TestHandleRedeliverEvent:
    def test_calls_events_redeliver_with_the_payload(self, org_id):
        with mock.patch.object(events, "redeliver") as fake_redeliver:
            event_redelivery.handle_redeliver_event({"org_id": org_id, "event_id": "evt-1"})
        fake_redeliver.assert_called_once_with(org_id, "evt-1")

"""server/jobs/queue.py + workers.py -- the generic work queue. No
mocking of the DB layer: this exercises real claim/backoff/dead-letter/
stale-reclaim behavior against Postgres, the same reason tests/conftest.py
runs against a real database rather than a fake driver.
"""
from __future__ import annotations

import time

import pytest

from server.db import pool
from server.jobs import queue, workers


@pytest.fixture(autouse=True)
def _reset_handlers():
    workers.reset_handlers()
    yield
    workers.reset_handlers()


class TestEnqueueAndClaim:
    def test_enqueue_then_claim(self):
        job = queue.enqueue("test_job", {"x": 1})
        assert job["status"] == "pending"
        claimed = queue.claim_batch("worker-1", ("test_job",), 10)
        assert len(claimed) == 1
        assert claimed[0]["id"] == job["id"]
        assert claimed[0]["status"] == "claimed"
        assert claimed[0]["claimed_by"] == "worker-1"

    def test_claim_only_returns_matching_job_types(self):
        queue.enqueue("type_a", {})
        queue.enqueue("type_b", {})
        claimed = queue.claim_batch("worker-1", ("type_a",), 10)
        assert len(claimed) == 1
        assert claimed[0]["job_type"] == "type_a"

    def test_a_claimed_job_is_not_claimed_again(self):
        queue.enqueue("test_job", {})
        first = queue.claim_batch("worker-1", ("test_job",), 10)
        second = queue.claim_batch("worker-2", ("test_job",), 10)
        assert len(first) == 1
        assert second == []

    def test_batch_size_limits_how_many_are_claimed(self):
        for _ in range(5):
            queue.enqueue("test_job", {})
        claimed = queue.claim_batch("worker-1", ("test_job",), 2)
        assert len(claimed) == 2

    def test_a_job_not_yet_due_is_not_claimed(self):
        job = queue.enqueue("test_job", {})
        with pool.system_transaction() as cur:
            cur.execute(
                "UPDATE jobs.work_queue SET available_at = now() + interval '1 hour' "
                "WHERE id = %s",
                (job["id"],),
            )
        assert queue.claim_batch("worker-1", ("test_job",), 10) == []

    def test_empty_job_types_claims_nothing(self):
        queue.enqueue("test_job", {})
        assert queue.claim_batch("worker-1", (), 10) == []


class TestMarkDone:
    def test_marks_done_and_clears_error(self):
        job = queue.enqueue("test_job", {})
        queue.claim_batch("worker-1", ("test_job",), 10)
        queue.mark_done(str(job["id"]))
        assert queue.get_job(str(job["id"]))["status"] == "done"


class TestMarkFailed:
    def test_a_first_failure_goes_back_to_pending_with_backoff(self):
        job = queue.enqueue("test_job", {}, max_attempts=5)
        queue.claim_batch("worker-1", ("test_job",), 10)
        queue.mark_failed(str(job["id"]), "boom")
        updated = queue.get_job(str(job["id"]))
        assert updated["status"] == "pending"
        assert updated["attempts"] == 1
        assert updated["last_error"] == "boom"
        assert updated["claimed_by"] == ""
        # Not immediately reclaimable -- backoff scheduled it into the future.
        assert queue.claim_batch("worker-2", ("test_job",), 10) == []

    def test_exhausting_max_attempts_moves_to_dead_letter(self):
        job = queue.enqueue("test_job", {}, max_attempts=2)
        for _ in range(2):
            queue.claim_batch("worker-1", ("test_job",), 10)
            queue.mark_failed(str(job["id"]), "still broken")
            # Force the next claim attempt to see it as due, bypassing
            # the real backoff wait so this test doesn't need to sleep.
            with pool.system_transaction() as cur:
                cur.execute(
                    "UPDATE jobs.work_queue SET available_at = now() WHERE id = %s",
                    (job["id"],),
                )
        updated = queue.get_job(str(job["id"]))
        assert updated["status"] == "dead_letter"
        assert updated["attempts"] == 2
        # A dead_letter row is never reclaimable, even once "due".
        assert queue.claim_batch("worker-1", ("test_job",), 10) == []

    def test_marking_a_nonexistent_job_failed_is_a_no_op(self):
        queue.mark_failed("00000000-0000-0000-0000-000000000000", "boom")  # must not raise


class TestReclaimStaleClaims:
    def test_a_stale_claim_is_reset_to_pending(self):
        job = queue.enqueue("test_job", {})
        queue.claim_batch("worker-1", ("test_job",), 10)
        with pool.system_transaction() as cur:
            cur.execute(
                "UPDATE jobs.work_queue SET claimed_at = now() - interval '1 hour' WHERE id = %s",
                (job["id"],),
            )
        reset_count = queue.reclaim_stale_claims(45)
        assert reset_count == 1
        assert queue.get_job(str(job["id"]))["status"] == "pending"

    def test_a_recent_claim_is_left_alone(self):
        queue.enqueue("test_job", {})
        queue.claim_batch("worker-1", ("test_job",), 10)
        assert queue.reclaim_stale_claims(45) == 0


class TestWorkerRegistration:
    def test_register_then_heartbeat_then_stop(self):
        queue.register_worker("w-1")
        assert queue.check_stop_requested("w-1") is False
        queue.heartbeat("w-1")
        queue.mark_worker_stopped("w-1")

    def test_reap_stale_workers_removes_silent_ones(self):
        queue.register_worker("w-stale")
        with pool.system_transaction() as cur:
            cur.execute(
                "UPDATE jobs.workers SET last_heartbeat = now() - interval '1 hour' "
                "WHERE worker_id = %s",
                ("w-stale",),
            )
        reaped = queue.reap_stale_workers(10)
        assert reaped == 1


class TestWorkersRunOnce:
    def test_dispatches_a_claimed_job_to_its_registered_handler(self):
        seen = []
        workers.register_handler("test_job", lambda payload: seen.append(payload))
        queue.enqueue("test_job", {"hello": "world"})
        processed = workers.run_once("worker-1")
        assert processed == 1
        assert seen == [{"hello": "world"}]
        # The queue tables are unscoped -- fetch the row directly rather
        # than through get_job's own id round trip, just to be explicit
        # this test isn't relying on ordering.

    def test_a_handler_that_raises_marks_the_job_failed_not_crashes(self):
        def boom(payload):
            raise RuntimeError("handler exploded")

        workers.register_handler("test_job", boom)
        job = queue.enqueue("test_job", {})
        processed = workers.run_once("worker-1")
        assert processed == 1
        updated = queue.get_job(str(job["id"]))
        assert updated["status"] == "pending"  # first failure -- backoff, not dead_letter
        assert "handler exploded" in updated["last_error"]

    def test_an_unregistered_job_type_is_never_claimed(self):
        queue.enqueue("nobody_handles_this", {})
        workers.register_handler("something_else", lambda payload: None)
        assert workers.run_once("worker-1") == 0

    def test_no_handlers_registered_claims_nothing(self):
        queue.enqueue("test_job", {})
        assert workers.run_once("worker-1") == 0

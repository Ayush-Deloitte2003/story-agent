import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.events import classify
from app.store import PostgresStore

REQUEST = {"flight_id": "SA101", "seat": "1A", "passenger_name": "Test", "hold_seconds": 60, "failure_at": None}


@unittest.skipUnless(os.getenv("DATABASE_URL") or os.getenv("REQUIRE_POSTGRES_TESTS"),
                     "requires PostgreSQL via DATABASE_URL")
class PostgresConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.getenv("DATABASE_URL"):
            raise RuntimeError("REQUIRE_POSTGRES_TESTS is set but DATABASE_URL is missing")
        cls.store = PostgresStore()
        cls.store.initialize()

    def test_simultaneous_identical_bookings_create_one_record_and_one_dispatch(self):
        key = f"key-{uuid.uuid4()}"
        workflow_id = "booking-" + uuid.uuid4().hex
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(
                lambda _: self.store.create_booking(workflow_id, key, dict(REQUEST)), range(8)))
        self.assertEqual({row["booking_id"] for row, _ in results}, {workflow_id})
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(self.store.dispatch_record(workflow_id)["status"], "pending")
        with self.assertRaises(ValueError):
            self.store.create_booking(workflow_id, key, {**REQUEST, "seat": "2B"})

    def test_simultaneous_duplicate_events_create_one_receipt_and_one_dispatch(self):
        event_id = f"evt-{uuid.uuid4()}"
        accepted = classify({"id": event_id, "source": "pg-test", "booking_id": "b", "flight_id": "SA101"})
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: self.store.accept_event(accepted), range(8)))
        self.assertEqual({workflow for workflow, _ in results}, {accepted.workflow_id})
        self.assertEqual(sum(created for _, created in results), 1)
        changed = classify({"id": event_id, "source": "pg-test", "booking_id": "b", "flight_id": "SA201"})
        with self.assertRaises(ValueError):
            self.store.accept_event(changed)

    def test_event_and_booking_with_same_idempotency_key_do_not_collide(self):
        key = f"shared-{uuid.uuid4()}"
        booking, created = self.store.create_booking("booking-" + uuid.uuid4().hex, key, dict(REQUEST))
        accepted = classify({"id": key, "source": key, "booking_id": "b", "flight_id": "SA101"})
        workflow_id, event_created = self.store.accept_event(accepted)
        self.assertTrue(created and event_created)
        self.assertNotEqual(workflow_id, booking["booking_id"])

    def test_two_replicas_cannot_claim_one_record(self):
        key = f"key-{uuid.uuid4()}"
        workflow_id = "booking-" + uuid.uuid4().hex
        self.store.create_booking(workflow_id, key, dict(REQUEST))
        with ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(
                lambda owner: self.store.lease_dispatches(owner, 10, 30, workflow_id), ("a", "b")))
        self.assertEqual(sorted(len(c) for c in claims), [0, 1])
        winner = "a" if claims[0] else "b"
        loser = "b" if winner == "a" else "a"
        self.assertFalse(self.store.complete_dispatch(workflow_id, loser))
        self.assertTrue(self.store.complete_dispatch(workflow_id, winner))
        self.assertEqual(self.store.lease_dispatches("c", 10, 30, workflow_id), [])

    def test_marker_survives_and_blocks_reinvocation_after_lease_expiry(self):
        from app import dispatcher
        workflow_id = "booking-" + uuid.uuid4().hex
        self.store.create_booking(workflow_id, f"key-{uuid.uuid4()}", dict(REQUEST))
        calls = []

        def invoke(_app, _method, body):
            calls.append(body)
            return {"scheduled": True, "already_existed": False}

        self.assertEqual(dispatcher.dispatch_due(self.store, invoke, dispatch_id=workflow_id, owner="a"), 1)
        with self.store._connect() as conn:  # simulate a replayed lease after Dapr history was purged
            conn.execute("UPDATE dispatch_records SET status = 'leased', lease_owner = 'z', "
                         "lease_expires_at = now() WHERE dispatch_id = %s", (workflow_id,))
        self.assertEqual(dispatcher.dispatch_due(self.store, invoke, dispatch_id=workflow_id, owner="b"), 1)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(self.store.dispatch_record(workflow_id)["scheduled_at"])

    def test_mark_scheduled_is_bound_to_the_lease_owner(self):
        workflow_id = "booking-" + uuid.uuid4().hex
        self.store.create_booking(workflow_id, f"key-{uuid.uuid4()}", dict(REQUEST))
        self.assertEqual(len(self.store.lease_dispatches("a", 10, 30, workflow_id)), 1)
        self.assertFalse(self.store.mark_scheduled(workflow_id, "b"))
        self.assertTrue(self.store.mark_scheduled(workflow_id, "a"))

    def test_additive_migration_backfills_marker_for_previously_dispatched_rows(self):
        workflow_id = "booking-" + uuid.uuid4().hex
        self.store.create_booking(workflow_id, f"key-{uuid.uuid4()}", dict(REQUEST))
        self.store.lease_dispatches("a", 10, 30, workflow_id)
        self.assertTrue(self.store.complete_dispatch(workflow_id, "a"))
        with self.store._connect() as conn:  # emulate the previous schema
            conn.execute("ALTER TABLE dispatch_records DROP COLUMN scheduled_at")
        self.store.initialize()
        self.store.initialize()  # re-running is a no-op
        self.assertIsNotNone(self.store.dispatch_record(workflow_id)["scheduled_at"])

    def test_expired_lease_is_reclaimed_and_started_workflow_is_detected(self):
        workflow_id = "booking-" + uuid.uuid4().hex
        self.store.create_booking(workflow_id, f"key-{uuid.uuid4()}", dict(REQUEST))
        first = self.store.lease_dispatches("a", 10, 0, workflow_id)
        second = self.store.lease_dispatches("b", 10, 30, workflow_id)
        self.assertEqual((len(first), len(second)), (1, 1))
        self.assertFalse(self.store.workflow_started(second[0]))
        self.store.mark_booking(workflow_id, "confirmed")
        self.assertTrue(self.store.workflow_started(second[0]))


if __name__ == "__main__":
    unittest.main()

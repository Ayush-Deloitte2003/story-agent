import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from app import ambient
from app.store import PostgresStore


@unittest.skipUnless(os.getenv("DATABASE_URL") or os.getenv("REQUIRE_POSTGRES_TESTS"),
                     "requires PostgreSQL via DATABASE_URL")
class AmbientIntakePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.getenv("DATABASE_URL"):
            raise RuntimeError("REQUIRE_POSTGRES_TESTS is set but DATABASE_URL is missing")
        cls.store = PostgresStore()
        cls.store.initialize()
        cls.store.initialize()  # additive migration is re-runnable

    def setUp(self):
        # Each test uses its own kind-scoped event ids; start the shared cursors at the current tail.
        for kind in ambient.KINDS:
            with self.store._connect() as conn:
                tail = conn.execute("SELECT COALESCE(max(seq), 0) AS m FROM ambient_events WHERE kind = %s",
                                    (kind,)).fetchone()["m"]
                conn.execute(
                    """INSERT INTO poller_cursors(name, last_seq) VALUES (%s, %s)
                       ON CONFLICT (name) DO UPDATE SET last_seq = EXCLUDED.last_seq""",
                    (ambient.cursor_name(kind), tail))

    def seed(self, kind, payload, event_id=None):
        event_id = event_id or f"evt-{uuid.uuid4().hex}"
        self.assertTrue(self.store.insert_ambient_event(kind, event_id, payload))
        return event_id

    def count(self, sql, *args):
        with self.store._connect() as conn:
            return conn.execute(sql, args).fetchone()["n"]

    def confirmed_booking(self):
        booking_id = "booking-" + uuid.uuid4().hex
        with self.store._connect() as conn:
            conn.execute("INSERT INTO bookings(booking_id, idempotency_key, request, status) "
                         "VALUES (%s, %s, '{}', 'confirmed')", (booking_id, booking_id))
            conn.execute("INSERT INTO tickets(booking_id, ticket_id, flight_id, seat) VALUES (%s, %s, 'SA101', '1A')",
                         (booking_id, "tkt-" + booking_id))
            conn.execute("INSERT INTO simulated_payments(booking_id, payment_id, amount_cents, status) "
                         "VALUES (%s, %s, 10000, 'paid')", (booking_id, "pay-" + booking_id))
        return booking_id

    def booking_event(self, **extra):
        return {"flight_id": "SA101", "seat": "1B", "passenger_name": "Sim Traveller", **extra}

    def test_booking_event_creates_one_booking_and_dispatch_and_replays_idempotently(self):
        event_id = self.seed("booking", self.booking_event())
        first = ambient.run_once(self.store, "booking")
        self.assertEqual((first.created, first.duplicates), (1, 0))
        plan = ambient.parse("booking", event_id, self.booking_event(), False)
        self.assertEqual(self.count("SELECT count(*) AS n FROM bookings WHERE booking_id = %s",
                                    plan.workflow_id), 1)
        self.assertEqual(self.count("SELECT count(*) AS n FROM dispatch_records WHERE dispatch_id = %s",
                                    plan.workflow_id), 1)
        # Crash before the cursor moved: rewind the cursor and replay the same event.
        with self.store._connect() as conn:
            conn.execute("UPDATE poller_cursors SET last_seq = last_seq - 1 WHERE name = %s",
                         (ambient.cursor_name("booking"),))
        replay = ambient.run_once(self.store, "booking")
        self.assertEqual((replay.created, replay.duplicates), (0, 1))
        self.assertEqual(self.count("SELECT count(*) AS n FROM bookings WHERE booking_id = %s",
                                    plan.workflow_id), 1)
        self.assertEqual(self.count("SELECT count(*) AS n FROM dispatch_records WHERE dispatch_id = %s",
                                    plan.workflow_id), 1)
        self.assertEqual(ambient.run_once(self.store, "booking").polled, 0)

    def test_concurrent_pollers_create_each_booking_once(self):
        ids = [self.seed("booking", self.booking_event(seat=f"{n}A")) for n in range(1, 6)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: ambient.run_once(self.store, "booking"), range(4)))
        self.assertEqual(sum(r.created for r in results), 5)
        workflow_ids = [ambient.parse("booking", i, self.booking_event(seat=f"{n}A"), False).workflow_id
                        for n, i in enumerate(ids, 1)]
        self.assertEqual(self.count("SELECT count(*) AS n FROM bookings WHERE booking_id = ANY(%s)",
                                    workflow_ids), 5)

    def test_invalid_event_is_quarantined_with_notify_item_and_cursor_advances(self):
        event_id = self.seed("booking", {"flight_id": "SA101", "seat": "1A"})  # no passenger_name
        stats = ambient.run_once(self.store, "booking")
        self.assertEqual(stats.quarantined, 1)
        self.assertEqual(self.count("SELECT count(*) AS n FROM quarantined_events WHERE event_id = %s",
                                    event_id), 1)
        items = [i for i in self.store.attention_items() if event_id[:64] in i["subject"]]
        self.assertEqual([i["kind"] for i in items], ["notify"])
        self.assertNotIn("Sim Traveller", items[0]["summary"])
        self.assertEqual(ambient.run_once(self.store, "booking").polled, 0)

    def test_missing_seat_raises_one_question_item_and_no_booking(self):
        event_id = self.seed("booking", {"flight_id": "SA101", "passenger_name": "Sim Traveller"})
        ambient.run_once(self.store, "booking")
        with self.store._connect() as conn:
            conn.execute("UPDATE poller_cursors SET last_seq = last_seq - 1 WHERE name = %s",
                         (ambient.cursor_name("booking"),))
        ambient.run_once(self.store, "booking")  # replay must not duplicate the item
        items = [i for i in self.store.attention_items() if event_id[:64] in i["subject"]]
        self.assertEqual([i["kind"] for i in items], ["question"])
        self.assertTrue(self.store.acknowledge_attention(items[0]["attention_id"]))
        self.assertFalse(self.store.acknowledge_attention("missing"))

    def test_cancellation_event_creates_one_cancellation_and_unknown_booking_is_quarantined(self):
        booking_id = self.confirmed_booking()
        event_id = self.seed("cancellation", {"booking_id": booking_id})
        missing = self.seed("cancellation", {"booking_id": "booking-" + uuid.uuid4().hex})
        stats = ambient.run_once(self.store, "cancellation")
        self.assertEqual((stats.created, stats.quarantined), (1, 1))
        plan = ambient.parse("cancellation", event_id, {"booking_id": booking_id}, False)
        self.assertEqual(self.count("SELECT count(*) AS n FROM cancellations WHERE workflow_id = %s",
                                    plan.workflow_id), 1)
        self.assertEqual(self.count("SELECT count(*) AS n FROM dispatch_records WHERE dispatch_id = %s",
                                    plan.workflow_id), 1)
        self.assertEqual(self.count("SELECT count(*) AS n FROM quarantined_events WHERE event_id = %s",
                                    missing), 1)
        self.assertEqual(self.count("SELECT count(*) AS n FROM approval_inbox WHERE booking_id = %s",
                                    booking_id), 0)  # the poller never approves anything

    def test_duplicate_source_event_id_is_not_inserted_twice(self):
        event_id = self.seed("booking", self.booking_event())
        self.assertFalse(self.store.insert_ambient_event("booking", event_id, self.booking_event()))


if __name__ == "__main__":
    unittest.main()

import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app import feed_poller
from app.store import PostgresStore


@unittest.skipUnless(os.getenv("DATABASE_URL") or os.getenv("REQUIRE_POSTGRES_TESTS"),
                     "requires PostgreSQL via DATABASE_URL")
class OpsFeedPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.getenv("DATABASE_URL"):
            raise RuntimeError("REQUIRE_POSTGRES_TESTS is set but DATABASE_URL is missing")
        cls.store = PostgresStore()
        cls.store.initialize()
        cls.store.initialize()  # additive migration is re-runnable

    def setUp(self):
        self.cursor = "test-" + uuid.uuid4().hex
        self.store.ensure_cursor(self.cursor, start_at_tail=True)
        self.flight = "T" + uuid.uuid4().hex[:10]
        with self.store._connect() as conn:
            conn.execute("""INSERT INTO flight_inventory(flight_id, origin, destination, depart_at, seats, price_cents)
                            VALUES (%s, 'AAA', 'BBB', '2030-01-01T00:00:00Z', '{}', 100)""", (self.flight,))

    def booking(self, status="confirmed", flight=None):
        booking_id = "booking-" + uuid.uuid4().hex
        with self.store._connect() as conn:
            conn.execute("INSERT INTO bookings(booking_id, idempotency_key, request, status) VALUES (%s, %s, '{}', %s)",
                         (booking_id, booking_id, status))
            conn.execute("INSERT INTO tickets(booking_id, ticket_id, flight_id, seat) VALUES (%s, %s, %s, '1A')",
                         (booking_id, "tkt-" + booking_id, flight or self.flight))
        return booking_id

    def feed(self, status="cancelled", delay=None, flight=None, payload=None):
        feed_id = "feed-" + uuid.uuid4().hex
        body = payload if payload is not None else {"flight_id": flight or self.flight, "status": status}
        if delay is not None and payload is None:
            body["delay_minutes"] = delay
        self.assertTrue(self.store.insert_feed_event(feed_id, body))
        return feed_id

    def poll(self, **kwargs):
        return feed_poller.run_once(self.store, cursor_name=self.cursor, **kwargs)

    def receipts(self, feed_id=None):
        with self.store._connect() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM event_receipts WHERE source = %s AND event_id LIKE %s",
                (feed_poller.FEED_SOURCE, (feed_id or "feed-") + "%:booking-%")).fetchone()["n"]

    def dispatches(self, booking_ids):
        with self.store._connect() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM dispatch_records WHERE payload->>'booking_id' = ANY(%s)",
                (booking_ids,)).fetchone()["n"]

    def quarantined(self, feed_ids):
        with self.store._connect() as conn:
            return conn.execute("SELECT count(*) AS n FROM quarantined_events WHERE event_id = ANY(%s)",
                                (feed_ids,)).fetchone()["n"]

    def test_unchanged_status_is_suppressed_and_real_changes_emit(self):
        bookings = [self.booking(), self.booking()]
        first = self.feed("cancelled")
        self.assertEqual(self.poll().emitted, 2)
        self.feed("cancelled")
        second = self.poll()
        self.assertEqual((second.emitted, second.suppressed), (0, 1))
        self.feed("on_time")
        self.assertEqual(self.poll().emitted, 0)
        self.feed("cancelled")
        again = self.poll()
        self.assertEqual(again.emitted, 2)  # a new transition is a new event
        self.assertEqual(self.dispatches(bookings), 4)
        self.assertEqual(self.receipts(first), 2)

    def test_delay_threshold_and_same_status_rules(self):
        self.booking()
        self.feed("delayed", 30)
        self.assertEqual(self.poll().emitted, 0)
        self.feed("delayed", 90)
        self.assertEqual(self.poll().emitted, 1)
        self.feed("delayed", 120)
        suppressed = self.poll()
        self.assertEqual((suppressed.emitted, suppressed.suppressed), (0, 1))

    def test_bad_rows_are_quarantined_and_never_stall_the_cursor(self):
        booking = self.booking()
        bad = [
            self.feed(payload={"flight_id": self.flight, "status": "exploded"}),
            self.feed(payload=["not", "an", "object"]),
            self.feed(payload={"flight_id": "NOPE-" + uuid.uuid4().hex[:6], "status": "cancelled"}),
            self.feed(payload={"flight_id": self.flight, "status": "cancelled", "extra": 1}),
        ]
        self.feed("cancelled")
        stats = self.poll()
        self.assertEqual((stats.quarantined, stats.emitted), (4, 1))
        self.assertEqual(self.quarantined(bad), 4)
        with self.store._connect() as conn:
            tail = conn.execute("SELECT max(seq) AS m FROM ops_feed_events").fetchone()["m"]
        self.assertEqual(self.store.cursor(self.cursor)["last_seq"], tail)
        self.assertEqual(self.poll().polled, 0)
        self.assertEqual(self.dispatches([booking]), 1)

    def test_restart_resumes_from_cursor_without_loss_or_duplicates(self):
        bookings = [self.booking() for _ in range(3)]
        self.feed("cancelled")
        self.feed("on_time")
        self.feed("cancelled")
        total = 0
        for index in range(6):
            store = PostgresStore() if index % 2 else self.store  # a fresh store models a restart
            total += feed_poller.run_once(store, batch_size=1, cursor_name=self.cursor).emitted
        self.assertEqual(total, 6)
        self.assertEqual(self.dispatches(bookings), 6)

    def test_crash_before_commit_rolls_back_and_replay_is_exact(self):
        bookings = [self.booking() for _ in range(3)]
        feed_id = self.feed("cancelled")
        before = self.store.cursor(self.cursor)
        real_emit, calls = feed_poller._emit, []

        def crash_after_one(*args):
            real_emit(*args)
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("crash")

        with patch.object(feed_poller, "_emit", crash_after_one), self.assertRaises(RuntimeError):
            self.poll()
        self.assertEqual(self.store.cursor(self.cursor), before)
        self.assertEqual(self.receipts(feed_id), 0)
        self.assertEqual(self.poll().emitted, 3)
        self.assertEqual(self.receipts(feed_id), 3)
        self.assertEqual(self.dispatches(bookings), 3)

    def test_crash_after_commit_before_next_pass_redelivery_creates_no_duplicate_workflow(self):
        bookings = [self.booking() for _ in range(2)]
        self.feed("cancelled")
        self.assertEqual(self.poll().emitted, 2)
        with self.store._connect() as conn:  # lose the cursor and the status memory
            seq = conn.execute("SELECT max(seq) AS m FROM ops_feed_events").fetchone()["m"]
            conn.execute("UPDATE poller_cursors SET last_seq = %s WHERE name = %s", (seq - 1, self.cursor))
            conn.execute("DELETE FROM ops_flight_status WHERE flight_id = %s", (self.flight,))
        again = self.poll()
        self.assertEqual((again.emitted, again.duplicates), (0, 2))
        self.assertEqual(self.dispatches(bookings), 2)

    def test_concurrent_pollers_emit_each_event_once(self):
        bookings = [self.booking() for _ in range(5)]
        self.feed("cancelled")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.poll(), range(4)))
        self.assertEqual(sum(r.emitted for r in results), 5)
        self.assertEqual(sum(r.duplicates for r in results), 0)
        self.assertEqual(self.dispatches(bookings), 5)

    def test_batch_cut_off_keeps_cursor_correct_and_resumes(self):
        bookings = [self.booking() for _ in range(5)]
        feed_id = self.feed("cancelled")
        follow_up = self.feed("on_time")
        with self.store._connect() as conn:
            seq = conn.execute("SELECT seq FROM ops_feed_events WHERE source_event_id = %s",
                               (feed_id,)).fetchone()["seq"]
        first = self.poll(max_events=2)
        self.assertEqual((first.emitted, first.truncated), (2, True))
        cursor = self.store.cursor(self.cursor)
        self.assertEqual((cursor["last_seq"] < seq, cursor["partial_seq"]), (True, seq))
        with self.store._connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM ops_flight_status WHERE flight_id = %s",
                                           (self.flight,)).fetchone())
        self.assertEqual(self.poll(max_events=2).emitted, 2)
        last = self.poll(max_events=2)
        self.assertEqual(last.emitted, 1)
        cursor = self.store.cursor(self.cursor)
        self.assertEqual((cursor["last_seq"] >= seq, cursor["partial_seq"]), (True, None))
        self.poll(max_events=2)
        self.assertEqual(self.store.cursor(self.cursor)["partial_seq"], None)
        self.assertEqual(self.dispatches(bookings), 5)
        self.assertEqual(self.receipts(feed_id), 5)
        self.assertEqual(self.poll().polled, 0)
        self.assertTrue(follow_up)

    def test_only_confirmed_bookings_on_the_flight_are_emitted_and_nothing_is_quarantined(self):
        confirmed = self.booking()
        self.booking(status="awaiting_approval")
        self.booking(flight="SA101")
        feed_id = self.feed("cancelled")
        stats = self.poll()
        self.assertEqual((stats.emitted, stats.quarantined), (1, 0))
        self.assertEqual(self.dispatches([confirmed]), 1)
        self.assertEqual(self.receipts(feed_id), 1)

    def test_poller_never_approves_or_changes_bookings(self):
        booking = self.booking()
        self.feed("cancelled")
        self.poll()
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM bookings WHERE booking_id = %s",
                                          (booking,)).fetchone()["status"], "confirmed")
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM approval_inbox WHERE booking_id = %s",
                                          (booking,)).fetchone()["n"], 0)

    def test_late_committing_lower_sequence_is_not_skipped(self):
        self.booking()
        slow = self.store._connect()
        try:
            slow.execute("INSERT INTO ops_feed_events(source_event_id, payload) VALUES (%s, %s)",
                         ("slow-" + uuid.uuid4().hex, '{"flight_id": "%s", "status": "on_time"}' % self.flight))
            self.feed("cancelled")  # higher seq commits first
            self.assertEqual(self.poll().polled, 0)
            slow.commit()
        finally:
            slow.close()
        stats = self.poll()
        self.assertEqual((stats.polled, stats.emitted), (2, 1))
        with self.store._connect() as conn:  # on_time (lower seq) was applied before cancelled
            status = conn.execute("SELECT status FROM ops_flight_status WHERE flight_id = %s",
                                  (self.flight,)).fetchone()["status"]
        self.assertEqual(status, "cancelled")


if __name__ == "__main__":
    unittest.main()

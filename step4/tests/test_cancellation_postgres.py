import json
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.inventory import refund_for
from app.store import PostgresStore

DEPART = "2030-01-01T00:00:00+00:00"


@unittest.skipUnless(os.getenv("DATABASE_URL") or os.getenv("REQUIRE_POSTGRES_TESTS"),
                     "requires PostgreSQL via DATABASE_URL")
class CancellationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.getenv("DATABASE_URL"):
            raise RuntimeError("REQUIRE_POSTGRES_TESTS is set but DATABASE_URL is missing")
        cls.store = PostgresStore()
        cls.store.initialize()
        cls.store.initialize()  # additive migration is re-runnable

    def flight(self, seats=("1A", "1B"), price=20000, depart=DEPART):
        flight_id = "C" + uuid.uuid4().hex[:10]
        with self.store._connect() as conn:
            conn.execute("""INSERT INTO flight_inventory(flight_id, origin, destination, depart_at, seats, price_cents)
                            VALUES (%s, 'AAA', 'BBB', %s, %s, %s)""",
                         (flight_id, depart, json.dumps(list(seats)), price))
        return flight_id

    def booking(self, flight_id, seat="1A", paid=20000, status="confirmed"):
        booking_id = "booking-" + uuid.uuid4().hex
        with self.store._connect() as conn:
            conn.execute("INSERT INTO bookings(booking_id, idempotency_key, request, status) VALUES (%s, %s, '{}', %s)",
                         (booking_id, booking_id, status))
            conn.execute("""INSERT INTO seat_holds(booking_id, flight_id, seat, expires_at, status)
                            VALUES (%s, %s, %s, now(), 'confirmed')""", (booking_id, flight_id, seat))
            conn.execute("INSERT INTO simulated_payments(booking_id, payment_id, amount_cents, status) "
                         "VALUES (%s, %s, %s, 'paid')", (booking_id, "pay-" + booking_id, paid))
            conn.execute("INSERT INTO tickets(booking_id, ticket_id, flight_id, seat) VALUES (%s, %s, %s, %s)",
                         (booking_id, "tkt-" + booking_id, flight_id, seat))
        return booking_id

    def new_booking(self, **kwargs):
        return self.booking(self.flight(**{k: v for k, v in kwargs.items() if k in ("price", "depart")}),
                            **{k: v for k, v in kwargs.items() if k in ("paid", "status")})

    def request(self, booking_id, **extra):
        return {"booking_id": booking_id, "approval_seconds": 300, "failure_at": None, **extra}

    def cancel(self, booking_id, key=None, **extra):
        key = key or "key-" + uuid.uuid4().hex
        workflow_id = "cancellation-" + uuid.uuid4().hex[:20]
        row, created = self.store.create_cancellation(workflow_id, key, self.request(booking_id, **extra))
        return row, created

    def proposal(self, row):
        snapshot = self.store.cancellation(row["workflow_id"])["snapshot"]
        fare = refund_for(snapshot["paid_cents"], snapshot["depart_at"], snapshot["requested_at"])
        return snapshot, fare, {"refund_cents": fare["refund_cents"], "fare_rule": fare["fare_rule"],
                                "request_hash": "h"}

    def open(self, row):
        snapshot, fare, proposal = self.proposal(row)
        result = self.store.open_cancellation_approval(
            row["workflow_id"], row["booking_id"], snapshot, "2099-01-01T00:00:00+00:00", proposal)
        return snapshot, fare, result

    def complete(self, row):
        snapshot, fare, _ = self.open(row)
        issued = self.store.issue_refund(row["workflow_id"], row["booking_id"], snapshot,
                                         fare["refund_cents"], None)
        released = self.store.release_cancelled_seat(row["workflow_id"], row["booking_id"], None)
        return issued, released

    def status(self, workflow_id):
        return self.store.cancellation(workflow_id)["status"]

    def dispatch_count(self, workflow_id):
        with self.store._connect() as conn:
            return conn.execute("SELECT count(*) AS n FROM dispatch_records             WHERE dispatch_id = %s",
                                (workflow_id,)).fetchone()["n"]

    # ---- intake ------------------------------------------------------------------------------

    def test_intake_snapshots_fare_inputs_and_creates_dispatch(self):
        booking_id = self.new_booking(paid=12345)
        row, created = self.cancel(booking_id)
        self.assertTrue(created)
        self.assertEqual(row["status"], "scheduled")
        snapshot = self.store.cancellation(row["workflow_id"])["snapshot"]
        self.assertEqual((snapshot["paid_cents"], snapshot["depart_at"]), (12345, DEPART))
        self.assertEqual(self.dispatch_count(row["workflow_id"]), 1)

    def test_same_key_replays_and_changed_payload_conflicts(self):
        booking_id = self.new_booking()
        row, _ = self.cancel(booking_id, key="k-" + booking_id)
        again, created = self.store.create_cancellation("cancellation-other", "k-" + booking_id,
                                                        self.request(booking_id))
        self.assertFalse(created)
        self.assertEqual(again["workflow_id"], row["workflow_id"])
        with self.assertRaises(ValueError):
            self.store.create_cancellation("cancellation-other", "k-" + booking_id,
                                           self.request(booking_id, approval_seconds=60))

    def test_simultaneous_duplicate_submissions_create_one_cancellation(self):
        booking_id = self.new_booking()
        key = "dup-" + booking_id

        def submit(_):
            return self.store.create_cancellation("cancellation-" + key, key, self.request(booking_id))

        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(submit, range(8)))
        self.assertEqual(sum(1 for _, created in results if created), 1)
        self.assertEqual(self.dispatch_count("cancellation-" + key), 1)

    def test_second_key_conflicts_while_one_is_active(self):
        booking_id = self.new_booking()
        self.cancel(booking_id)
        with self.assertRaises(ValueError):
            self.cancel(booking_id)

    def test_concurrent_different_keys_allow_only_one_active(self):
        booking_id = self.new_booking()

        def submit(i):
            try:
                return self.cancel(booking_id, key=f"race-{booking_id}-{i}")[1]
            except ValueError:
                return None

        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(submit, range(6)))
        self.assertEqual(results.count(True), 1)

    def test_unknown_unconfirmed_and_cancelled_bookings_are_refused(self):
        with self.assertRaises(LookupError):
            self.cancel("booking-missing")
        with self.assertRaises(ValueError):
            self.cancel(self.new_booking(status="pending"))
        booking_id = self.new_booking()
        row, _ = self.cancel(booking_id)
        self.complete(row)
        with self.assertRaises(ValueError):
            self.cancel(booking_id)

    # ---- refund and release ------------------------------------------------------------------

    def test_full_flow_refunds_releases_and_is_idempotent(self):
        flight_id = self.flight()
        booking_id = self.booking(flight_id)
        row, _ = self.cancel(booking_id)
        issued, released = self.complete(row)
        self.assertEqual((issued["result"], issued["amount_cents"]), ("issued", 20000))
        self.assertFalse(released["already_released"])
        snapshot, fare, _ = self.proposal(row)
        # Replays after a restart or a Dapr redelivery change nothing.
        again = self.store.issue_refund(row["workflow_id"], booking_id, snapshot, fare["refund_cents"], None)
        self.assertEqual(again["refund_id"], issued["refund_id"])
        self.assertTrue(self.store.release_cancelled_seat(row["workflow_id"], booking_id, None)["already_released"])
        self.assertEqual(self.store.booking(booking_id)["status"], "cancelled")
        self.assertEqual(self.status(row["workflow_id"]), "completed")
        self.assertEqual(self.store.refund(row["workflow_id"])["status"], "issued")
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM simulated_payments WHERE booking_id = %s",
                                          (booking_id,)).fetchone()["status"], "refunded")
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM seat_holds WHERE booking_id = %s",
                                          (booking_id,)).fetchone()["n"], 0)
        self.assertEqual(self.store.inbox_item(row["workflow_id"])["status"], "completed")

    def test_released_seat_can_be_booked_again(self):
        flight_id = self.flight()
        booking_id = self.booking(flight_id, seat="1A")
        row, _ = self.cancel(booking_id)
        self.complete(row)
        self.booking(flight_id, seat="1A")  # unique seat index would raise if the hold remained

    def test_zero_refund_tier_leaves_payment_paid(self):
        booking_id = self.new_booking(depart="2000-01-01T00:00:00+00:00")
        row, _ = self.cancel(booking_id)
        issued, _ = self.complete(row)
        self.assertEqual(issued["amount_cents"], 0)
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM simulated_payments WHERE booking_id = %s",
                                          (booking_id,)).fetchone()["status"], "paid")

    def test_refund_amount_uses_snapshot_not_later_catalog_changes(self):
        flight_id = self.flight(price=20000)
        booking_id = self.booking(flight_id, paid=20000)
        row, _ = self.cancel(booking_id)
        snapshot, fare, _ = self.proposal(row)
        with self.store._connect() as conn:
            conn.execute("UPDATE flight_inventory SET price_cents = 99999, depart_at = %s WHERE flight_id = %s",
                         ("2000-01-01T00:00:00+00:00", flight_id))
        self.assertEqual(self.store.cancellation(row["workflow_id"])["snapshot"], snapshot)
        recomputed = refund_for(snapshot["paid_cents"], snapshot["depart_at"], snapshot["requested_at"])
        self.assertEqual(recomputed, fare)
        self.assertEqual(fare["refund_cents"], 20000)
        issued = self.store.issue_refund(row["workflow_id"], booking_id, snapshot, fare["refund_cents"], None)
        self.assertEqual(issued["amount_cents"], 20000)

    # ---- races -------------------------------------------------------------------------------

    def test_rebook_behaves_unchanged_without_active_cancellation(self):
        source = self.flight()
        target = self.flight(seats=("2A",))
        booking_id = self.booking(source)
        result = self.store.rebook(booking_id, "rebook-" + uuid.uuid4().hex, target)
        self.assertEqual((result["ticket"]["flight_id"], result["ticket"]["seat"]), (target, "2A"))

    def test_rebook_is_refused_while_cancellation_is_active(self):
        source, target = self.flight(), self.flight(seats=("2A",))
        booking_id = self.booking(source)
        self.cancel(booking_id)
        with self.assertRaisesRegex(RuntimeError, "cancellation is in progress"):
            self.store.rebook(booking_id, "rebook-" + uuid.uuid4().hex, target)

    def test_cancellation_completed_first_blocks_rebook(self):
        source, target = self.flight(), self.flight(seats=("2A",))
        booking_id = self.booking(source)
        row, _ = self.cancel(booking_id)
        self.complete(row)
        with self.assertRaisesRegex(RuntimeError, "Only confirmed"):
            self.store.rebook(booking_id, "rebook-" + uuid.uuid4().hex, target)

    def test_rebook_completed_first_makes_cancellation_stale(self):
        source, target = self.flight(), self.flight(seats=("2A",))
        booking_id = self.booking(source)
        row, _ = self.cancel(booking_id)
        snapshot = self.store.cancellation(row["workflow_id"])["snapshot"]
        with self.store._connect() as conn:  # simulate a rebook that finished before approval opened
            conn.execute("UPDATE cancellations SET status = 'completed' WHERE workflow_id = %s", (row["workflow_id"],))
        self.store.rebook(booking_id, "rebook-" + uuid.uuid4().hex, target)
        with self.store._connect() as conn:
            conn.execute("UPDATE cancellations SET status = 'scheduled' WHERE workflow_id = %s", (row["workflow_id"],))
        _, _, result = self.open(row)
        self.assertEqual(result, {"opened": False, "status": "stale"})
        self.assertEqual(self.status(row["workflow_id"]), "stale")
        issued = self.store.issue_refund(row["workflow_id"], booking_id, snapshot, 20000, None)
        self.assertEqual(issued, {"result": "stale"})

    def test_unconfirmed_booking_cannot_open_approval(self):
        booking_id = self.new_booking()
        row, _ = self.cancel(booking_id)
        with self.store._connect() as conn:
            conn.execute("UPDATE bookings SET status = 'failed' WHERE booking_id = %s", (booking_id,))
        _, _, result = self.open(row)
        self.assertEqual(result["status"], "not_confirmed")

    # ---- failure, compensation, and the one-active rule --------------------------------------

    def test_injected_failures_raise_on_every_attempt(self):
        booking_id = self.new_booking()
        row, _ = self.cancel(booking_id)
        snapshot, fare, _ = self.proposal(row)
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                self.store.issue_refund(row["workflow_id"], booking_id, snapshot, 20000, "refund")
        self.assertIsNone(self.store.refund(row["workflow_id"]))
        self.store.issue_refund(row["workflow_id"], booking_id, snapshot, 20000, None)
        with self.assertRaises(RuntimeError):
            self.store.release_cancelled_seat(row["workflow_id"], booking_id, "release")

    def test_release_failure_reverses_refund_and_keeps_booking_confirmed(self):
        booking_id = self.new_booking()
        row, _ = self.cancel(booking_id)
        snapshot, fare, _ = self.open(row)
        self.store.issue_refund(row["workflow_id"], booking_id, snapshot, fare["refund_cents"], None)
        self.store.reverse_refund(row["workflow_id"], booking_id)
        self.store.reverse_refund(row["workflow_id"], booking_id)  # idempotent
        self.assertEqual(self.store.refund(row["workflow_id"])["status"], "reversed")
        self.assertEqual(self.status(row["workflow_id"]), "failed")
        self.assertEqual(self.store.booking(booking_id)["status"], "confirmed")
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM simulated_payments WHERE booking_id = %s",
                                          (booking_id,)).fetchone()["status"], "paid")

    def test_new_request_allowed_after_every_terminal_failure_status(self):
        for terminal in ("refund_failed", "failed", "rejected", "expired", "stale", "not_confirmed"):
            with self.subTest(terminal=terminal):
                booking_id = self.new_booking()
                row, _ = self.cancel(booking_id)
                self.open(row)
                self.store.close_cancellation(row["workflow_id"], terminal)
                self.assertEqual(self.status(row["workflow_id"]), terminal)
                retry, created = self.cancel(booking_id)
                self.assertTrue(created)
                self.assertEqual(retry["status"], "scheduled")

    def test_retry_after_reversed_refund_can_complete(self):
        booking_id = self.new_booking()
        first, _ = self.cancel(booking_id)
        snapshot, fare, _ = self.open(first)
        self.store.issue_refund(first["workflow_id"], booking_id, snapshot, fare["refund_cents"], None)
        self.store.reverse_refund(first["workflow_id"], booking_id)
        second, _ = self.cancel(booking_id)
        issued, released = self.complete(second)
        self.assertEqual(issued["result"], "issued")
        self.assertEqual(released["status"], "cancelled")

    def test_close_cancellation_does_not_overwrite_completed(self):
        booking_id = self.new_booking()
        row, _ = self.cancel(booking_id)
        self.complete(row)
        self.store.close_cancellation(row["workflow_id"], "expired")
        self.assertEqual(self.status(row["workflow_id"]), "completed")


if __name__ == "__main__":
    unittest.main()

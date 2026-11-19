import json
import os
import unittest
import uuid
from unittest.mock import patch

os.environ.setdefault("AGENT_MODE", "mock")

from app import support, support_service
from app.inventory import deterministic_fare_advice
from app.store import PostgresStore

TABLES = ("bookings", "cancellations", "refunds", "approval_inbox", "dispatch_records",
          "event_receipts", "quarantined_events", "simulated_payments", "tickets", "seat_holds")


@unittest.skipUnless(os.getenv("DATABASE_URL") or os.getenv("REQUIRE_POSTGRES_TESTS"),
                     "requires PostgreSQL via DATABASE_URL")
class SupportPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.getenv("DATABASE_URL"):
            raise RuntimeError("REQUIRE_POSTGRES_TESTS is set but DATABASE_URL is missing")
        cls.store = PostgresStore()
        cls.store.initialize()

    def confirmed_booking(self, passenger="Zed Unmistakable"):
        flight_id = "S" + uuid.uuid4().hex[:10]
        booking_id = "booking-" + uuid.uuid4().hex
        with self.store._connect() as conn:
            conn.execute("""INSERT INTO flight_inventory(flight_id, origin, destination, depart_at, seats, price_cents)
                            VALUES (%s, 'AAA', 'BBB', '2030-01-01T00:00:00+00:00', '["1A"]', 20000)""", (flight_id,))
            conn.execute("INSERT INTO bookings(booking_id, idempotency_key, request, status) VALUES (%s, %s, %s, 'confirmed')",
                         (booking_id, booking_id, json.dumps({"flight_id": flight_id, "seat": "1A",
                                                              "passenger_name": passenger})))
            conn.execute("INSERT INTO seat_holds(booking_id, flight_id, seat, expires_at, status) "
                         "VALUES (%s, %s, '1A', now(), 'confirmed')", (booking_id, flight_id))
            conn.execute("INSERT INTO simulated_payments(booking_id, payment_id, amount_cents, status) "
                         "VALUES (%s, %s, 20000, 'paid')", (booking_id, "pay-" + booking_id))
            conn.execute("INSERT INTO tickets(booking_id, ticket_id, flight_id, seat) VALUES (%s, %s, %s, '1A')",
                         (booking_id, "tkt-" + booking_id, flight_id))
        return booking_id

    def counts(self):
        with self.store._connect() as conn:
            return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in TABLES}

    def test_facts_are_passenger_free_and_missing_booking_is_none(self):
        booking_id = self.confirmed_booking()
        facts = self.store.support_facts(booking_id)
        self.assertEqual((facts["booking_status"], facts["paid_cents"], facts["origin"]), ("confirmed", 20000, "AAA"))
        self.assertNotIn("Unmistakable", json.dumps(facts))
        self.assertIsNone(self.store.support_facts("booking-missing"))

    def test_active_cancellation_removes_the_cancel_suggestion(self):
        booking_id = self.confirmed_booking()
        self.assertIn("request_cancellation", support.allowed_actions(self.store.support_facts(booking_id)))
        self.store.create_cancellation("cancellation-" + uuid.uuid4().hex[:20], "k-" + uuid.uuid4().hex,
                                       {"booking_id": booking_id, "approval_seconds": 300, "failure_at": None})
        facts = self.store.support_facts(booking_id)
        self.assertEqual(facts["cancellation_status"], "scheduled")
        self.assertNotIn("request_cancellation", support.allowed_actions(facts))

    def test_support_question_writes_nothing(self):
        booking_id = self.confirmed_booking()
        before = self.counts()
        with patch.object(support_service, "store", self.store), \
                patch.object(support_service, "_call_advisor", side_effect=TimeoutError("slow")):
            result = support_service.ask(support_service.SupportQuestion(
                booking_id=booking_id, question="Can I cancel for a refund?"))
        self.assertEqual(result["source"], "fallback")
        self.assertEqual(result["suggested_action"], "request_cancellation")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.booking(booking_id)["status"], "confirmed")

    def test_fare_advice_round_trips_in_the_approval_proposal_only(self):
        booking_id = self.confirmed_booking()
        workflow_id = "wf-" + uuid.uuid4().hex
        advice = deterministic_fare_advice("SA101")
        self.store.create_booking_approval(
            workflow_id, booking_id, "2031-01-01T00:00:00Z",
            {"flight_id": "SA101", "fare_advice": advice, "fare_advice_note": None})
        item = self.store.inbox_item(workflow_id)
        self.assertEqual(item["proposal"]["fare_advice"]["ranked_flight_ids"], advice["ranked_flight_ids"])
        self.assertEqual(self.store.booking(booking_id)["ticket"]["seat"], "1A")


if __name__ == "__main__":
    unittest.main()

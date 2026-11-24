import unittest
from unittest.mock import patch

from app import ambient
from app.events import Quarantined


class AmbientParseTests(unittest.TestCase):
    def test_enabled_kinds_defaults_off_and_ignores_unknown(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(ambient.enabled_kinds(), [])
        self.assertEqual(ambient.enabled_kinds("cancellation, booking, other"), ["booking", "cancellation"])

    def test_booking_event_yields_deterministic_plan(self):
        body = {"flight_id": "SA101", "seat": "1A", "passenger_name": "Sim Traveller"}
        first = ambient.parse("booking", "evt-1", body, False)
        again = ambient.parse("booking", "evt-1", body, False)
        other = ambient.parse("booking", "evt-2", body, False)
        self.assertIsInstance(first, ambient.Plan)
        self.assertEqual(first, again)
        self.assertNotEqual(first.workflow_id, other.workflow_id)
        self.assertTrue(first.workflow_id.startswith("booking-"))
        self.assertEqual(first.payload["flight_id"], "SA101")

    def test_missing_seat_is_a_question_not_a_booking(self):
        outcome = ambient.parse("booking", "evt-1", {"flight_id": "SA101", "passenger_name": "X"}, False)
        self.assertIsInstance(outcome, ambient.Question)

    def test_bad_events_are_quarantined_without_raising(self):
        cases = [
            ("booking", "e", "not-an-object"),
            ("booking", "e" * 200, {"flight_id": "SA101", "seat": "1A", "passenger_name": "X"}),
            ("booking", "e", {"flight_id": "SA101", "seat": "1A"}),
            ("booking", "e", {"flight_id": "SA101", "seat": "1A", "passenger_name": "X", "failure_at": "payment"}),
            ("cancellation", "e", {"booking_id": "b", "surprise": 1}),
        ]
        for kind, event_id, payload in cases:
            with self.subTest(payload=payload):
                self.assertIsInstance(ambient.parse(kind, event_id, payload, False), Quarantined)

    def test_failure_injection_only_when_enabled(self):
        body = {"flight_id": "SA101", "seat": "1A", "passenger_name": "X", "failure_at": "payment"}
        self.assertIsInstance(ambient.parse("booking", "e", body, True), ambient.Plan)

    def test_support_event_parses_and_rejects_bad_payloads(self):
        ok = ambient.parse("support", "s1", {"booking_id": "booking-1", "question": "Can I cancel?"}, False)
        self.assertEqual(ok, ambient.SupportPlan("booking-1", "Can I cancel?"))
        for payload in ({"booking_id": "b"}, {"booking_id": "b", "question": "q", "extra": 1},
                        {"booking_id": "b", "question": "x" * 501}):
            with self.subTest(payload=payload):
                self.assertIsInstance(ambient.parse("support", "s1", payload, False), Quarantined)

    def test_cancellation_event_yields_plan(self):
        outcome = ambient.parse("cancellation", "evt-9", {"booking_id": "booking-abc"}, False)
        self.assertIsInstance(outcome, ambient.Plan)
        self.assertTrue(outcome.workflow_id.startswith("cancellation-"))


if __name__ == "__main__":
    unittest.main()

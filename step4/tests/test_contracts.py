import unittest

from app.contracts import BookingRequest, booking_payload
from app.booking_advisor import advise_booking
from app.inventory import recommend_alternatives


class ContractTests(unittest.TestCase):
    def test_failure_injection_is_off_unless_explicitly_enabled(self):
        request = BookingRequest(flight_id="SA101", seat="1A", passenger_name="Test", failure_at="payment")
        with self.assertRaisesRegex(ValueError, "disabled"):
            booking_payload(request, False)
        self.assertEqual(booking_payload(request, True)["failure_at"], "payment")

    def test_recommendations_only_include_same_route_mock_flights(self):
        self.assertEqual(
            [item["flight_id"] for item in recommend_alternatives("SA101")],
            ["SA102"],
        )
        self.assertEqual(recommend_alternatives("missing"), [])

    def test_request_rejects_unbounded_hold(self):
        with self.assertRaises(Exception):
            BookingRequest(flight_id="SA101", seat="1A", passenger_name="Test", hold_seconds=3601)

    def test_booking_advisor_is_read_only_and_marks_advice_non_authoritative(self):
        advice = advise_booking("SA101", "1A")
        self.assertEqual(advice["authority"], "advisory_only")
        self.assertEqual(advice["recommendation"], "eligible_for_human_review")
        self.assertEqual([item["flight_id"] for item in advice["same_route_alternatives"]], ["SA102"])
        self.assertNotIn("hold", advice)
        self.assertNotIn("payment", advice)
        self.assertNotIn("ticket", advice)
        self.assertEqual(advise_booking("SA101", "9Z")["recommendation"], "manual_review")


if __name__ == "__main__":
    unittest.main()

if __name__ == "__main__":
    unittest.main()

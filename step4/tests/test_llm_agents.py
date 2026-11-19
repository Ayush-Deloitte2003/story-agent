import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from app import llm
from app.advisor import advise_rebooking
from app.agent_contracts import (
    validate_booking_advice,
    validate_disruption_advice,
    validate_payment_risk_advice,
)
from app.booking_advisor import advise_booking
from app.payment_advisor import assess_payment_risk


class FakeResponse:
    def __init__(self, value):
        self.body = json.dumps({
            "choices": [{"message": {"content": json.dumps(value)}}]
        }).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return self.body


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def open(self, _request, timeout):
        self.calls += 1
        if self.error:
            raise self.error
        if timeout != 20:
            raise AssertionError("Unexpected provider timeout")
        return self.response


class LLMAgentTests(unittest.TestCase):
    def setUp(self):
        with llm._request_lock:
            self.original_requests = llm._provider_requests
            llm._provider_requests = 0
        self.environment = patch.dict(os.environ, {
            "AGENT_MODE": "llm",
            "OPENAI_API_KEY": "test-key-not-a-secret",
            "LLM_REQUEST_LIMIT": "5",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def tearDown(self):
        with llm._request_lock:
            llm._provider_requests = self.original_requests

    def test_json_response_is_parsed_once(self):
        opener = FakeOpener(response=FakeResponse({"ok": True}))
        with patch("app.llm.urllib.request.build_opener", return_value=opener) as build:
            self.assertEqual(llm.complete_json("prompt", {"flight_id": "SA101"}), {"ok": True})
        self.assertEqual(opener.calls, 1)
        build.assert_called_once_with(llm._NoRedirectHandler)
        self.assertEqual(llm.provider_request_count(), 1)

    def test_provider_failure_is_not_retried(self):
        error = urllib.error.HTTPError("https://example.invalid", 429, "limited", {}, None)
        opener = FakeOpener(error=error)
        with patch("app.llm.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(llm.LLMRequestError, "HTTP 429"):
                llm.complete_json("prompt", {})
        self.assertEqual(opener.calls, 1)
        self.assertEqual(llm.provider_request_count(), 1)

    def test_hard_cap_stops_after_five_provider_requests(self):
        opener = FakeOpener(response=FakeResponse({"ok": True}))
        with patch("app.llm.urllib.request.build_opener", return_value=opener):
            for _ in range(5):
                llm.complete_json("prompt", {})
            with self.assertRaisesRegex(llm.LLMRequestError, "cap reached"):
                llm.complete_json("prompt", {})
        self.assertEqual(opener.calls, 5)
        self.assertEqual(llm.provider_request_count(), 5)

    def test_advisors_accept_only_bounded_structured_recommendations(self):
        with patch("app.booking_advisor.complete_json", return_value={
            "recommendation": "eligible_for_human_review",
            "summary": "The requested flight and seat are listed.",
            "alternative_flight_ids": ["SA102"],
        }):
            booking = advise_booking("SA101", "1A")
        self.assertEqual(booking["authority"], "advisory_only")
        self.assertEqual([item["flight_id"] for item in booking["same_route_alternatives"]], ["SA102"])

        with patch("app.payment_advisor.complete_json", return_value={
            "risk_level": "review", "rationale": "The amount merits human review.",
        }):
            payment = assess_payment_risk("SA101", "1A", 12_000)
        self.assertEqual(payment["authority"], "advisory_only")

        with patch("app.advisor.complete_json", return_value={
            "recommended_flight_id": "SA102", "rationale": "It is the listed same-route option.",
        }):
            disruption = advise_rebooking("SA101")
        self.assertEqual(disruption["recommended_flight_id"], "SA102")
        self.assertEqual(disruption["authority"], "advisory_only")

        with patch("app.booking_advisor.complete_json", return_value={
            "recommendation": "eligible_for_human_review",
            "summary": "Choose a different flight.",
            "alternative_flight_ids": ["SA201"],
        }):
            with self.assertRaisesRegex(ValueError, "unknown alternative"):
                advise_booking("SA101", "1A")

    def test_workflow_contracts_reject_authority_and_catalog_changes(self):
        with self.assertRaisesRegex(ValueError, "advisory"):
            validate_booking_advice({
                "recommendation": "eligible_for_human_review",
                "reason": "Looks good.",
                "authority": "book_now",
            }, "SA101", "1A")
        with self.assertRaisesRegex(ValueError, "shape"):
            validate_payment_risk_advice({
                "risk_level": "low", "rationale": "Looks fine.",
                "authority": "advisory_only", "charge": True,
            })
        with self.assertRaisesRegex(ValueError, "alternatives"):
            validate_disruption_advice({
                "booking_id": "b-1",
                "flight_id": "SA101",
                "alternatives": [],
                "recommended_flight_id": "SA201",
                "rationale": "Pick this.",
                "authority": "advisory_only",
            }, "b-1", "SA101")


if __name__ == "__main__":
    unittest.main()

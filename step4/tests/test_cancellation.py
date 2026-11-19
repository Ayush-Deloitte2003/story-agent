import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("AGENT_MODE", "mock")

from fastapi import HTTPException

from app import api, cancellation_advisor
from app.agent_contracts import validate_cancellation_advice
from app.contracts import (
    CancellationRequest, DecisionRequest, cancellation_payload, cancellation_request_hash,
)
from app.events import Quarantined, classify
from app.inventory import refund_for

DEPART = "2027-03-10T12:00:00+00:00"
SNAPSHOT = {"flight_id": "SA201", "seat": "1A", "paid_cents": 20000, "depart_at": DEPART,
            "requested_at": "2027-03-01T12:00:00+00:00"}


class RefundFareRuleTests(unittest.TestCase):
    def refund(self, hours_before, paid=20000):
        from datetime import datetime, timedelta
        depart = datetime.fromisoformat(DEPART)
        return refund_for(paid, DEPART, (depart - timedelta(hours=hours_before)).isoformat())

    def test_tiers_and_boundaries(self):
        self.assertEqual(self.refund(200)["refund_cents"], 20000)
        self.assertEqual(self.refund(72)["fare_rule"], "full_refund_72h")
        self.assertEqual(self.refund(71.9)["refund_cents"], 10000)
        self.assertEqual(self.refund(24)["fare_rule"], "half_refund_24h")
        self.assertEqual(self.refund(23.9), {"fare_rule": "no_refund_under_24h", "eligibility": "ineligible",
                                              "refund_percent": 0, "refund_cents": 0})
        self.assertEqual(self.refund(-5)["refund_cents"], 0)

    def test_rounding_is_integer_floor(self):
        self.assertEqual(self.refund(30, paid=999)["refund_cents"], 499)

    def test_naive_timestamps_are_rejected(self):
        with self.assertRaises(ValueError):
            refund_for(100, "2027-03-10T12:00:00", "2027-03-01T12:00:00+00:00")


class AdviceValidationTests(unittest.TestCase):
    def setUp(self):
        self.fare = refund_for(SNAPSHOT["paid_cents"], DEPART, SNAPSHOT["requested_at"])
        self.advice = cancellation_advisor.advise_cancellation(
            "SA201", SNAPSHOT["paid_cents"], DEPART, SNAPSHOT["requested_at"])

    def test_mock_advice_matches_deterministic_rules(self):
        self.assertEqual(validate_cancellation_advice(self.advice, self.fare), self.advice)
        self.assertEqual(self.advice["authority"], "advisory_only")

    def test_mismatching_or_malformed_advice_is_rejected(self):
        bad_cases = [
            {**self.advice, "proposed_refund_cents": self.fare["refund_cents"] + 1},
            {**self.advice, "proposed_refund_cents": True},
            {**self.advice, "fare_rule": "half_refund_24h"},
            {**self.advice, "eligibility": "ineligible"},
            {**self.advice, "authority": "approve"},
            {**self.advice, "rationale": "x" * 181},
            {**self.advice, "extra": 1},
        ]
        for case in bad_cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                validate_cancellation_advice(case, self.fare)

    def test_llm_mode_output_is_validated(self):
        wrong = {"eligibility": "eligible", "proposed_refund_cents": 1, "fare_rule": "full_refund_72h",
                 "rationale": "r"}
        with patch.dict(os.environ, {"AGENT_MODE": "llm"}), patch(
                "app.cancellation_advisor.complete_json", return_value=wrong):
            with self.assertRaises(ValueError):
                cancellation_advisor.advise_cancellation("SA201", 20000, DEPART, SNAPSHOT["requested_at"])


class RequestHashTests(unittest.TestCase):
    def test_hash_binds_request_seat_and_amount(self):
        base = cancellation_request_hash("w1", "b1", SNAPSHOT, 20000, "full_refund_72h")
        self.assertEqual(base, cancellation_request_hash("w1", "b1", SNAPSHOT, 20000, "full_refund_72h"))
        self.assertNotEqual(base, cancellation_request_hash("w2", "b1", SNAPSHOT, 20000, "full_refund_72h"))
        self.assertNotEqual(base, cancellation_request_hash("w1", "b1", SNAPSHOT, 10000, "full_refund_72h"))
        self.assertNotEqual(base, cancellation_request_hash("w1", "b1", {**SNAPSHOT, "seat": "2A"}, 20000,
                                                            "full_refund_72h"))

    def test_failure_injection_requires_flag(self):
        request = CancellationRequest(booking_id="b", failure_at="refund")
        with self.assertRaises(ValueError):
            cancellation_payload(request, False)
        self.assertEqual(cancellation_payload(request, True)["failure_at"], "refund")


class CancellationEndpointTests(unittest.TestCase):
    def test_accepts_with_202_shape_and_dispatches(self):
        with (
            patch.object(api.store, "create_cancellation",
                         return_value=({"booking_id": "b1", "status": "scheduled"}, True)),
            patch("app.api._try_dispatch") as dispatch,
        ):
            result = api.create_cancellation(CancellationRequest(booking_id="b1"), "key-1")
        self.assertEqual(result["status"], "scheduled")
        self.assertEqual(result["booking_id"], "b1")
        self.assertTrue(result["workflow_id"].startswith("cancellation-"))
        dispatch.assert_called_once_with(result["workflow_id"])

    def test_error_mapping(self):
        for exc, code in ((LookupError("no"), 404), (ValueError("conflict"), 409)):
            with patch.object(api.store, "create_cancellation", side_effect=exc):
                with self.assertRaises(HTTPException) as raised:
                    api.create_cancellation(CancellationRequest(booking_id="b1"), "key-1")
            self.assertEqual(raised.exception.status_code, code)

    def test_external_cancellation_event_is_quarantined(self):
        outcome = classify({"id": "e1", "source": "s", "type": "com.example.cancellation.requested",
                            "data": {"workflow_id": "w", "booking_id": "b"}})
        self.assertIsInstance(outcome, Quarantined)


class CancellationDecisionTests(unittest.TestCase):
    PROPOSAL = {"request_hash": "h" * 64, "refund_cents": 20000}

    def decide(self, kind, request, proposal=None):
        item = {"kind": kind, "proposal": proposal or self.PROPOSAL}
        with (
            patch.object(api.store, "inbox_item", return_value=item),
            patch.object(api.store, "decide", return_value=True) as store_decide,
            patch("app.api._invoke") as invoke,
        ):
            result = api.decide("w1", request)
        return result, store_decide, invoke

    def test_approval_without_hash_or_amount_is_rejected(self):
        for request in (DecisionRequest(decision="approve"),
                        DecisionRequest(decision="approve", request_hash="h" * 64),
                        DecisionRequest(decision="approve", refund_cents=20000)):
            with self.subTest(request=request), self.assertRaises(HTTPException) as raised:
                self.decide("cancellation", request)
            self.assertEqual(raised.exception.status_code, 422)

    def test_mismatched_hash_or_amount_conflicts(self):
        for request in (DecisionRequest(decision="approve", request_hash="x" * 64, refund_cents=20000),
                        DecisionRequest(decision="approve", request_hash="h" * 64, refund_cents=19999)):
            with self.subTest(request=request), self.assertRaises(HTTPException) as raised:
                self.decide("cancellation", request)
            self.assertEqual(raised.exception.status_code, 409)

    def test_matching_approval_goes_to_cancellation_worker(self):
        request = DecisionRequest(decision="approve", request_hash="h" * 64, refund_cents=20000)
        _, store_decide, invoke = self.decide("cancellation", request)
        store_decide.assert_called_once_with("w1", {"decision": "approve", "request_hash": "h" * 64,
                                                    "refund_cents": 20000})
        self.assertEqual(invoke.call_args.args[0], "cancellation-worker")

    def test_cancellation_reject_needs_no_binding_and_rejects_flight(self):
        _, store_decide, _ = self.decide("cancellation", DecisionRequest(decision="reject"))
        store_decide.assert_called_once_with("w1", {"decision": "reject"})
        with self.assertRaises(HTTPException) as raised:
            self.decide("cancellation", DecisionRequest(decision="reject", flight_id="SA101"))
        self.assertEqual(raised.exception.status_code, 422)

    def test_booking_and_rebooking_approvals_are_unchanged(self):
        # Cancellation-only fields are dropped so stored decisions look exactly as before.
        _, store_decide, invoke = self.decide(
            "booking", DecisionRequest(decision="approve", request_hash="h", refund_cents=5))
        store_decide.assert_called_once_with("w1", {"decision": "approve"})
        self.assertEqual(invoke.call_args.args[0], "booking-worker")
        proposal = {"alternatives": [{"flight_id": "SA103"}]}
        _, store_decide, invoke = self.decide(
            "rebooking", DecisionRequest(decision="approve", flight_id="SA103", refund_cents=5), proposal)
        store_decide.assert_called_once_with("w1", {"decision": "approve", "flight_id": "SA103"})
        self.assertEqual(invoke.call_args.args[0], "disruption-worker")
        with self.assertRaises(HTTPException) as raised:
            self.decide("rebooking", DecisionRequest(decision="approve", flight_id="SA999"), proposal)
        self.assertEqual(raised.exception.status_code, 422)
        with self.assertRaises(HTTPException) as raised:
            self.decide("booking", DecisionRequest(decision="approve", flight_id="SA103"))
        self.assertEqual(raised.exception.status_code, 422)


class ManifestTests(unittest.TestCase):
    def test_cancellation_worker_is_mock_by_default(self):
        text = (Path(__file__).resolve().parents[1] / "k8s" / "apps.yaml").read_text()
        block = text.split("name: cancellation-worker", 1)[1].split("kind: Service", 1)[0]
        self.assertIn("cancellation-advisor", block)
        self.assertIn('{ name: AGENT_MODE, value: "mock" }', block)
        self.assertIn("optional: true", block)
        self.assertNotIn("ENABLE_FAILURE_INJECTION", block)


if __name__ == "__main__":
    unittest.main()

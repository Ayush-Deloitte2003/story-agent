import ast
import asyncio
import inspect
import logging
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("AGENT_MODE", "mock")

from fastapi import HTTPException

from app import booking_workflow, fare_advisor, support, support_advisor, support_service
from app.agent_contracts import validate_fare_advice, validate_support_advice
from app.inventory import compare_options, deterministic_fare_advice

STEP4 = Path(__file__).resolve().parents[1]
FACTS = {"booking_status": "confirmed", "flight_id": "SA101", "origin": "SFO", "destination": "SEA",
         "depart_at": "2027-06-01T09:00:00Z", "paid_cents": 18000, "payment_status": "paid",
         "cancellation_status": None, "refund_status": None, "refund_cents": None}


class FareCatalogTests(unittest.TestCase):
    def test_comparison_lists_requested_flight_first_with_catalog_values(self):
        options = compare_options("SA101")
        self.assertEqual([o["flight_id"] for o in options], ["SA101", "SA102"])
        self.assertEqual(options[0]["connection_risk"], "low")
        self.assertEqual(options[1]["connection_risk"], "medium")
        self.assertEqual(options[1]["price_cents"], 21000)
        self.assertEqual(compare_options("NOPE"), [])

    def test_flights_endpoint_data_is_unchanged(self):
        from app.inventory import FLIGHTS
        self.assertEqual(set(FLIGHTS["SA101"]), {"origin", "destination", "depart_at", "seats", "price_cents"})


class FareValidationTests(unittest.TestCase):
    def setUp(self):
        self.advice = fare_advisor.advise_fare("SA101")

    def test_mock_advice_is_valid_and_advisory(self):
        self.assertEqual(validate_fare_advice(self.advice, "SA101"), self.advice)
        self.assertEqual(self.advice["authority"], "advisory_only")

    def test_unknown_flight_gives_empty_valid_comparison(self):
        advice = fare_advisor.advise_fare("NOPE")
        self.assertEqual(advice["comparison"], [])

    def test_fabricated_or_malformed_advice_is_rejected(self):
        comparison = self.advice["comparison"]
        changed_price = [{**comparison[0], "price_cents": 1}, *comparison[1:]]
        bad_cases = [
            {**self.advice, "comparison": changed_price},
            {**self.advice, "comparison": list(reversed(comparison))},
            {**self.advice, "ranked_flight_ids": ["SA101"]},
            {**self.advice, "ranked_flight_ids": ["SA101", "SA101"]},
            {**self.advice, "ranked_flight_ids": ["SA101", "SA999"]},
            {**self.advice, "authority": "binding"},
            {**self.advice, "summary": ""},
            {**self.advice, "summary": "x" * 241},
            {**self.advice, "book_now": "SA102"},
            "not a dict",
        ]
        for case in bad_cases:
            with self.subTest(case=str(case)[:60]), self.assertRaises(ValueError):
                validate_fare_advice(case, "SA101")

    def test_llm_mode_rejects_invented_ranking_and_extra_fields(self):
        with patch.dict(os.environ, {"AGENT_MODE": "llm"}):
            with patch.object(fare_advisor, "complete_json",
                              return_value={"ranked_flight_ids": ["SA101", "SA555"], "summary": "ok"}):
                with self.assertRaises(ValueError):
                    fare_advisor.advise_fare("SA101")
            with patch.object(fare_advisor, "complete_json",
                              return_value={"ranked_flight_ids": ["SA102", "SA101"], "summary": "ok",
                                            "selected": "SA102"}):
                with self.assertRaises(ValueError):
                    fare_advisor.advise_fare("SA101")
            with patch.object(fare_advisor, "complete_json",
                              return_value={"ranked_flight_ids": ["SA102", "SA101"], "summary": "ok"}):
                self.assertEqual(fare_advisor.advise_fare("SA101")["ranked_flight_ids"], ["SA102", "SA101"])


class BookingIntegrationTests(unittest.TestCase):
    def test_fare_advice_is_proposal_only_and_does_not_change_the_flight(self):
        captured = {}

        class FakeStore:
            def create_booking_approval(self, workflow_id, booking_id, expires_at, proposal):
                captured["proposal"] = proposal

        advice = deterministic_fare_advice("SA101")
        advice["ranked_flight_ids"] = ["SA102", "SA101"]  # even a preference for another flight is inert
        request = {"flight_id": "SA101", "seat": "1A", "passenger_name": "Test Passenger", "hold_seconds": 60}
        with patch.object(booking_workflow, "store", FakeStore()):
            asyncio.run(booking_workflow.open_booking_approval.__wrapped__(None, {
                "workflow_id": "w", "booking_id": "b", "request": request, "expires_at": "2027-01-01T00:00:00Z",
                "advice": {}, "payment_advice": {}, "fare_advice": advice, "fare_advice_note": None}))
        self.assertEqual(captured["proposal"]["flight_id"], "SA101")
        self.assertEqual(captured["proposal"]["fare_advice"]["ranked_flight_ids"], ["SA102", "SA101"])

    def test_workflow_never_feeds_fare_advice_into_booking_actions(self):
        tree = ast.parse(inspect.getsource(booking_workflow.flight_booking_workflow))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "call_activity" and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in {"hold_seat", "simulate_payment", "issue_ticket"}):
                self.assertNotIn("fare_advice", ast.dump(node))

    def test_fare_activity_has_no_retry_and_failure_paths_fall_back(self):
        source = inspect.getsource(booking_workflow.flight_booking_workflow)
        self.assertIn("deterministic_fare_advice", source)
        self.assertIn('"advisor_unavailable"', source)
        fallback = deterministic_fare_advice("SA101")
        self.assertEqual(validate_fare_advice(fallback, "SA101"), fallback)


class SupportHelperTests(unittest.TestCase):
    def test_scrub_is_best_effort_and_bounded(self):
        scrubbed = support.scrub_question("mail a.b@example.com card 4111 1111 1111 1111 " + "x" * 600)
        self.assertNotIn("example.com", scrubbed)
        self.assertNotIn("4111", scrubbed)
        self.assertLessEqual(len(scrubbed), support.MAX_QUESTION_CHARS)

    def test_allowed_actions_follow_booking_state(self):
        self.assertIn("request_cancellation", support.allowed_actions(FACTS))
        self.assertNotIn("request_cancellation", support.allowed_actions({**FACTS, "booking_status": "cancelled"}))
        self.assertNotIn("request_cancellation",
                         support.allowed_actions({**FACTS, "cancellation_status": "awaiting_approval"}))

    def test_validator_rejects_unallowed_or_malformed_advice(self):
        ok = {"answer": "Fine.", "suggested_action": "none", "authority": "advisory_only"}
        self.assertEqual(validate_support_advice(ok, ["none"]), ok)
        for bad in ({**ok, "suggested_action": "request_cancellation"}, {**ok, "suggested_action": "refund_now"},
                    {**ok, "authority": "approve"}, {**ok, "answer": "x" * 601}, {**ok, "extra": 1},
                    {**ok, "answer": ""}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_support_advice(bad, ["none"])

    def test_mock_advisor_suggests_only_an_action_never_performs_one(self):
        actions = support.allowed_actions(FACTS)
        preview = support.refund_preview(FACTS)
        result = support_advisor.advise_support(FACTS, "Can I cancel for a refund?", actions, preview)
        self.assertEqual(result["suggested_action"], "request_cancellation")
        self.assertEqual(result["authority"], "advisory_only")
        self.assertIn("human must approve", result["answer"])


class SupportServiceTests(unittest.TestCase):
    QUESTION = "Please refund my card 4111111111111111 SECRETWORD"

    def setUp(self):
        facts = dict(FACTS)

        class FakeStore:
            writes = 0

            def support_facts(self, booking_id):
                return facts if booking_id == "b1" else None

        self.store_patch = patch.object(support_service, "store", FakeStore())
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        for key in support_service.counters:
            support_service.counters[key] = 0

    def ask(self, booking_id="b1"):
        return support_service.ask(support_service.SupportQuestion(booking_id=booking_id, question=self.QUESTION))

    def test_advisor_answer_is_returned_with_scrubbed_question_only(self):
        seen = {}

        def advisor(payload):
            seen.update(payload)
            return support_advisor.advise_support(payload["facts"], payload["question"],
                                                  payload["allowed_actions"], payload["refund_preview"])

        with patch.object(support_service, "_call_advisor", advisor):
            result = self.ask()
        self.assertEqual(result["source"], "advisor")
        self.assertTrue(result["requires_human_approval"])
        self.assertNotIn("4111", seen["question"])
        self.assertNotIn("passenger", " ".join(seen["facts"]))

    def test_unavailable_or_invalid_advisor_falls_back_deterministically(self):
        bad_advice = {"answer": "Refunded!", "suggested_action": "refund_now", "authority": "advisory_only"}

        def raising(exc):
            def call(payload):
                raise exc
            return call

        for call in (raising(TimeoutError("slow")), raising(ValueError("bad")), lambda payload: bad_advice):
            with patch.object(support_service, "_call_advisor", call):
                result = self.ask()
            self.assertEqual(result["source"], "fallback")
            self.assertEqual(result["authority"], "advisory_only")
            self.assertIn(result["suggested_action"], support.allowed_actions(FACTS))
        self.assertEqual(support_service.counters["fallback"], 3)

    def test_unknown_booking_is_404(self):
        with self.assertRaises(HTTPException) as caught:
            self.ask("missing")
        self.assertEqual(caught.exception.status_code, 404)

    def test_questions_and_answers_are_never_logged(self):
        with self.assertLogs("step4.support", level=logging.INFO) as logs:
            with patch.object(support_service, "_call_advisor", side_effect=TimeoutError("slow")):
                result = self.ask()
        output = "\n".join(logs.output)
        self.assertNotIn("SECRETWORD", output)
        self.assertNotIn("4111", output)
        self.assertNotIn(result["answer_draft"], output)
        self.assertIn("source=fallback", output)

    def test_support_service_source_has_no_writes(self):
        for module in (support_service, support):
            text = inspect.getsource(module).upper()
            for verb in ("INSERT ", "UPDATE ", "DELETE ", "_CONNECT"):
                self.assertNotIn(verb, text, module.__name__)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.text = (STEP4 / "k8s" / "apps.yaml").read_text()

    def block(self, name):
        return self.text.split(f"name: {name}\n", 1)[1].split("\n---", 1)[0]

    def test_new_advisors_are_mock_by_default(self):
        for deployment in ("booking-worker", "support-service"):
            block = self.block(deployment)
            self.assertIn('{ name: AGENT_MODE, value: "mock" }', block)
            self.assertNotIn('value: "llm"', block)
        self.assertIn("fare-advisor", self.block("booking-worker"))
        self.assertIn("support-advisor", self.block("support-service"))

    def test_support_runs_outside_the_api_pod_and_without_dapr(self):
        self.assertNotIn("support", self.block("flight-api"))
        self.assertNotIn("dapr.io", self.block("support-service"))


if __name__ == "__main__":
    unittest.main()

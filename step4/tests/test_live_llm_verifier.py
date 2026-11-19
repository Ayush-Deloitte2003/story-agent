import ast
import inspect
import json
import os
import unittest
from unittest.mock import patch

from app.booking_workflow import flight_booking_workflow
from app.disruption_workflow import flight_rebooking_workflow
from scripts import verify_live_llm


class FakeFlightApi:
    def __init__(self):
        self.booking_approved = False
        self.rebooking_approved = False

    def __call__(self, method, path, body=None, headers=None, expected=(200, 202)):
        if method == "GET" and path == "/healthz":
            return {}
        if method == "POST" and path == "/bookings":
            self.assert_idempotency(headers)
            return {"booking_id": "booking-1", "workflow_id": "booking-workflow-1"}
        if method == "POST" and path == "/disruptions":
            return {"workflow_id": "disruption-workflow-1"}
        if method == "POST" and path == "/inbox/booking-workflow-1/decision":
            self.booking_approved = True
            return {"status": "decision_recorded"}
        if method == "POST" and path == "/inbox/disruption-workflow-1/decision":
            self.rebooking_approved = True
            return {"status": "decision_recorded"}
        if method == "GET" and path == "/bookings/booking-1":
            if self.rebooking_approved:
                status, ticket_flight = "confirmed", "SA102"
            elif self.booking_approved:
                status, ticket_flight = "confirmed", "SA101"
            else:
                status, ticket_flight = "awaiting_approval", None
            workflow = {"custom_status": json.dumps({"stage": status})}
            booking = {
                "status": status,
                "ticket": {"flight_id": ticket_flight} if ticket_flight else None,
                "payment": {"status": "paid"} if ticket_flight else None,
            }
            return {"booking": booking, "workflow": workflow}
        if method == "GET" and path == "/inbox":
            items = []
            if not self.booking_approved:
                items.append(self.booking_item("pending"))
            if self.booking_approved and not self.rebooking_approved:
                items.append(self.rebooking_item("pending"))
            return {"items": items}
        if method == "GET" and path == "/inbox/booking-workflow-1":
            approved = self.booking_approved
            return {
                "item": self.booking_item("approved" if approved else "pending"),
                "workflow": self.workflow_state(
                    "COMPLETED" if approved else "RUNNING",
                    "awaiting_approval",
                    {"status": "confirmed"} if approved else None,
                ),
            }
        if method == "GET" and path == "/inbox/disruption-workflow-1":
            completed = self.rebooking_approved
            return {
                "item": self.rebooking_item("completed" if completed else "pending"),
                "workflow": self.workflow_state(
                    "COMPLETED" if completed else "RUNNING",
                    "awaiting_rebooking_approval",
                    {"status": "rebooked"} if completed else None,
                ),
            }
        raise AssertionError(f"Unexpected API request: {method} {path}")

    @staticmethod
    def assert_idempotency(headers):
        if headers != {"Idempotency-Key": "step4-live-llm-42"}:
            raise AssertionError("Unexpected booking idempotency key")

    @staticmethod
    def booking_item(status):
        return {
            "workflow_id": "booking-workflow-1",
            "kind": "booking",
            "status": status,
            "proposal": {
                "booking_advice": {"authority": "advisory_only"},
                "payment_risk_advice": {"authority": "advisory_only"},
            },
        }

    @staticmethod
    def rebooking_item(status):
        return {
            "workflow_id": "disruption-workflow-1",
            "kind": "rebooking",
            "status": status,
            "proposal": {
                "alternatives": [{"flight_id": "SA102"}],
                "recommended_flight_id": "SA102",
                "authority": "advisory_only",
            },
        }

    @staticmethod
    def workflow_state(runtime_status, stage, output):
        return {
            "runtime_status": runtime_status,
            "custom_status": json.dumps({"stage": stage}),
            "output": json.dumps(output) if output else None,
        }


class LiveLLMVerifierTests(unittest.TestCase):
    def test_verifier_exercises_booking_approval_and_completed_rebooking(self):
        fake_api = FakeFlightApi()
        with (
            patch.object(verify_live_llm, "request", side_effect=fake_api),
            patch.dict(os.environ, {"GITHUB_RUN_ID": "42"}),
            patch("builtins.print"),
        ):
            verify_live_llm.main()
        self.assertTrue(fake_api.booking_approved)
        self.assertTrue(fake_api.rebooking_approved)

    def test_llm_workflow_activities_have_no_retry_policy(self):
        targets = (
            (flight_booking_workflow, {"booking_policy_advice", "payment_risk_advice"}),
            (flight_rebooking_workflow, {"recommend_rebooking"}),
        )
        for workflow, llm_activities in targets:
            tree = ast.parse(inspect.getsource(workflow))
            found = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "call_activity" or not node.args:
                    continue
                activity = node.args[0]
                if isinstance(activity, ast.Name) and activity.id in llm_activities:
                    found.add(activity.id)
                    self.assertNotIn("retry_policy", {kw.arg for kw in node.keywords})
            self.assertEqual(found, llm_activities)


if __name__ == "__main__":
    unittest.main()

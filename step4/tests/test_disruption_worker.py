import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app import disruption_worker


class DisruptionWorkerLifespanTests(unittest.IsolatedAsyncioTestCase):
    def test_worker_declares_its_disruption_subscription(self):
        self.assertEqual(
            disruption_worker.subscriptions(),
            [{"pubsubname": "pubsub", "topic": "flight-disruption",
              "route": "/events/flight-disruption"}],
        )

    async def test_fastapi_lifespan_starts_and_stops_dapr_runtime(self):
        self.assertIs(disruption_worker.app.router.lifespan_context, disruption_worker.lifespan)
        with (
            patch.object(disruption_worker.runtime, "start") as start,
            patch.object(disruption_worker.runtime, "shutdown") as shutdown,
            patch("app.disruption_worker.wf.DaprWorkflowClient", return_value=object()),
        ):
            async with disruption_worker.app.router.lifespan_context(disruption_worker.app):
                start.assert_called_once()
            shutdown.assert_called_once()

    async def test_workflow_state_endpoint_exposes_durable_rebooking_status(self):
        state = SimpleNamespace(
            runtime_status=SimpleNamespace(name="COMPLETED"),
            serialized_custom_status='{"stage":"awaiting_rebooking_approval"}',
            serialized_output='{"status":"rebooked"}',
            failure_details=None,
        )
        client = SimpleNamespace(get_workflow_state=lambda *_args, **_kwargs: state)
        with patch.object(disruption_worker, "client", client):
            result = await disruption_worker.workflow_state("workflow-1")
        self.assertEqual(result["runtime_status"], "COMPLETED")
        self.assertEqual(result["output"], '{"status":"rebooked"}')

    async def test_workflow_state_endpoint_returns_404_for_missing_state(self):
        client = SimpleNamespace(get_workflow_state=lambda *_args, **_kwargs: None)
        with patch.object(disruption_worker, "client", client):
            with self.assertRaises(HTTPException) as raised:
                await disruption_worker.workflow_state("missing")
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()

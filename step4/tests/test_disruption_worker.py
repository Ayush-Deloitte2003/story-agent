import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()

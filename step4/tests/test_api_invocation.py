import io
import unittest
import urllib.error
from unittest.mock import patch

from fastapi import HTTPException

from app import api
from app.api import _invoke


class ApiInvocationTests(unittest.TestCase):
    def test_inbox_detail_returns_current_approval_state(self):
        item = {"workflow_id": "workflow-1", "status": "completed", "kind": "rebooking"}
        workflow = {"runtime_status": "COMPLETED"}
        with (
            patch.object(api.store, "inbox_item", return_value=item),
            patch("app.api._invoke", return_value=workflow) as invoke,
        ):
            self.assertEqual(api.inbox_detail("workflow-1"), {"item": item, "workflow": workflow})
        invoke.assert_called_once_with(
            "disruption-worker", "internal/workflows/workflow-1", http_method="GET"
        )

    def test_booking_inbox_detail_reads_booking_worker_state(self):
        item = {"workflow_id": "booking-1", "status": "pending", "kind": "booking"}
        with (
            patch.object(api.store, "inbox_item", return_value=item),
            patch("app.api._invoke", return_value={"runtime_status": "RUNNING"}) as invoke,
        ):
            api.inbox_detail("booking-1")
        invoke.assert_called_once_with(
            "booking-worker", "internal/workflows/booking-1", http_method="GET"
        )

    def test_inbox_detail_rejects_unknown_workflow(self):
        with patch.object(api.store, "inbox_item", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                api.inbox_detail("missing")
        self.assertEqual(raised.exception.status_code, 404)

    def test_retries_transient_dapr_service_discovery_failure(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"raised":true}'

        unavailable = urllib.error.HTTPError(
            "http://127.0.0.1:3500", 503, "Unavailable", {}, io.BytesIO(b"")
        )
        with (
            patch("app.api.urllib.request.urlopen", side_effect=[unavailable, Response()]) as urlopen,
            patch("app.api.time.sleep") as sleep,
        ):
            self.assertEqual(_invoke("booking-worker", "internal/approval/raise"), {"raised": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()

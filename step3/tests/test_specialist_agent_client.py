import io
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from app.specialist_agent_client import review_with_sidecar


class AgentSidecarClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_review_input_and_returns_agent_result(self):
        body = json.dumps({"consistent": True}).encode()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = body

        with patch("app.specialist_agent_client.urllib.request.urlopen", return_value=response) as urlopen:
            result = await review_with_sidecar("The Lantern", "A short story.", "Help a neighbor.")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8100/review")
        self.assertEqual(json.loads(request.data), {
            "title": "The Lantern",
            "story": "A short story.",
            "request": "Help a neighbor.",
        })
        self.assertEqual(result, {"consistent": True})

    async def test_rejects_non_object_agent_result(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"[]"

        with patch("app.specialist_agent_client.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "non-object response"):
                await review_with_sidecar("The Lantern", "A short story.", "")

    async def test_surfaces_agent_http_failures(self):
        error = HTTPError(
            "http://127.0.0.1:8100/review",
            503,
            "unavailable",
            {},
            io.BytesIO(b"agent unavailable"),
        )
        with patch("app.specialist_agent_client.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 503: agent unavailable"):
                await review_with_sidecar("The Lantern", "A short story.", "")

import io
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from app.story_agent_client import draft_with_sidecar, review_quality_with_sidecar


class StoryAgentClientTests(unittest.IsolatedAsyncioTestCase):
    def response(self, body):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(body).encode()
        return response

    async def test_posts_draft_request_and_revisions(self):
        draft = {"title": "The Lantern", "story": "A short story.", "llm_calls": 2}
        with patch(
            "app.story_agent_client.urllib.request.urlopen",
            return_value=self.response(draft),
        ) as urlopen:
            result = await draft_with_sidecar(
                "Write a story.", [{"draft": {"title": "Old"}, "feedback": "Revise"}]
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8100/draft")
        self.assertEqual(json.loads(request.data), {
            "request": "Write a story.",
            "revisions": [{"draft": {"title": "Old"}, "feedback": "Revise"}],
        })
        self.assertEqual(result, draft)

    async def test_posts_quality_review_to_its_sidecar(self):
        review = {
            "score": 4,
            "recommendation": "ready",
            "summary": "A coherent story.",
            "strengths": ["Clear ending."],
            "issues": [],
            "suggestions": [],
        }
        with patch(
            "app.story_agent_client.urllib.request.urlopen",
            return_value=self.response(review),
        ) as urlopen:
            result = await review_quality_with_sidecar(
                "The Lantern", "A short story.", "Help a neighbor."
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8101/review")
        self.assertEqual(json.loads(request.data), {
            "title": "The Lantern",
            "story": "A short story.",
            "request": "Help a neighbor.",
        })
        self.assertEqual(result, review)

    async def test_rejects_invalid_quality_review(self):
        with patch(
            "app.story_agent_client.urllib.request.urlopen",
            return_value=self.response({"score": 9}),
        ):
            with self.assertRaisesRegex(ValueError, "score must be an integer"):
                await review_quality_with_sidecar("The Lantern", "Text", "")

    async def test_rejects_malformed_draft(self):
        with patch(
            "app.story_agent_client.urllib.request.urlopen",
            return_value=self.response({"title": "The Lantern", "story": "Text"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid LLM call count"):
                await draft_with_sidecar("Write a story.", [])

    async def test_surfaces_sidecar_http_failures(self):
        error = HTTPError(
            "http://127.0.0.1:8101/review",
            503,
            "unavailable",
            {},
            io.BytesIO(b"agent unavailable"),
        )
        with patch("app.story_agent_client.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 503: agent unavailable"):
                await review_quality_with_sidecar("The Lantern", "Text", "")

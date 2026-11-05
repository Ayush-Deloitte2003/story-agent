import importlib
import hashlib
import json
import subprocess
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _install_import_stubs() -> None:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    fastmcp = types.ModuleType("mcp.server.fastmcp")

    class FastMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self):
            return lambda function: function

        def run(self, *args, **kwargs):
            pass

    fastmcp.FastMCP = FastMCP
    mcp = types.ModuleType("mcp")
    server = types.ModuleType("mcp.server")
    mcp.server = server
    server.fastmcp = fastmcp
    sys.modules.update({
        "dotenv": dotenv,
        "mcp": mcp,
        "mcp.server": server,
        "mcp.server.fastmcp": fastmcp,
    })


_install_import_stubs()
from mcp_server import publisher


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        publisher.STATE_FILE = self.root / "attempts.json"
        publisher.PUBLISH_DIR = self.root / "published"
        publisher.FAIL_TIMES = 1

    def tearDown(self):
        self.temp.cleanup()

    def test_restart_preserves_attempts_and_retry_is_idempotent(self):
        with self.assertRaisesRegex(RuntimeError, "attempt 1"):
            publisher.publish_story("A Story", "Approved text")

        importlib.reload(publisher)
        publisher.STATE_FILE = self.root / "attempts.json"
        publisher.PUBLISH_DIR = self.root / "published"
        publisher.FAIL_TIMES = 1
        importlib.reload(publisher)
        publisher.STATE_FILE = self.root / "attempts.json"
        publisher.PUBLISH_DIR = self.root / "published"
        publisher.FAIL_TIMES = 1
        first_result = publisher.publish_story("A Story", "Approved text")
        importlib.reload(publisher)
        publisher.STATE_FILE = self.root / "attempts.json"
        publisher.PUBLISH_DIR = self.root / "published"
        publisher.FAIL_TIMES = 1
        repeated_result = publisher.publish_story("A Story", "Approved text")

        self.assertIn("attempt 2", first_result)
        self.assertEqual(first_result, repeated_result)
        self.assertEqual(len(list(publisher.PUBLISH_DIR.glob("*.md"))), 1)
        self.assertEqual(next(publisher.PUBLISH_DIR.glob("*.md")).read_text(encoding="utf-8"),
                         "# A Story\n\nApproved text\n")

    def test_concurrent_retries_create_one_publication(self):
        publisher.FAIL_TIMES = 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _: publisher.publish_story(
                    "Concurrent", "One approved draft", idempotency_key="workflow-concurrent"
                ),
                range(8),
            ))

        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(list(publisher.PUBLISH_DIR.glob("*.md"))), 1)
        state = publisher._load_state()
        self.assertEqual(list(state["attempts"].values()), [1])

    def test_workflow_idempotency_survives_publisher_restart(self):
        publisher.FAIL_TIMES = 0
        first_result = publisher.publish_story("Restart", "Approved text", "workflow-restart")

        importlib.reload(publisher)
        publisher.STATE_FILE = self.root / "attempts.json"
        publisher.PUBLISH_DIR = self.root / "published"
        publisher.FAIL_TIMES = 0
        repeated_result = publisher.publish_story("Restart", "Approved text", "workflow-restart")

        self.assertEqual(first_result, repeated_result)
        self.assertEqual(len(list(publisher.PUBLISH_DIR.glob("*.md"))), 1)

    def test_workflow_idempotency_key_deduplicates_content_and_rejects_reuse(self):
        publisher.FAIL_TIMES = 0
        result = publisher.publish_story("First title", "Approved text", "workflow-123")

        self.assertEqual(
            publisher.publish_story("First title", "Approved text", "workflow-123"),
            result,
        )
        self.assertEqual(len(list(publisher.PUBLISH_DIR.glob("*.md"))), 1)
        with self.assertRaisesRegex(ValueError, "different story content"):
            publisher.publish_story("Changed title", "Changed text", "workflow-123")

    def test_invalid_workflow_idempotency_key_is_rejected(self):
        for key in ("", " ", "x" * 513):
            with self.subTest(key_length=len(key)), self.assertRaises(ValueError):
                publisher.publish_story("A Story", "Approved text", key)

    def test_published_markdown_matches_approved_draft_exactly(self):
        approved = self.root / "approved.json"
        markdown = self.root / "published.md"
        approved.write_text(json.dumps({"title": "Approved", "story": "Exact text"}), encoding="utf-8")
        markdown.write_text("# Approved\n\nExact text\n", encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "verify_published_story.py"

        result = subprocess.run(
            [sys.executable, str(script), str(approved), str(markdown)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        markdown.write_text("# Approved\n\nChanged text\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(script), str(approved), str(markdown)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_publisher_state_verifier_uses_workflow_idempotency_key(self):
        workflow_id = "story-event-workflow-123"
        key = hashlib.sha256(f"workflow:{workflow_id}".encode()).hexdigest()
        approved = self.root / "approved.json"
        state_file = self.root / "attempts.json"
        approved.write_text(json.dumps({
            "title": "Approved",
            "story": "Exact text",
            "workflow_id": workflow_id,
        }), encoding="utf-8")
        state_file.write_text(json.dumps({
            "attempts": {key: 8},
            "published": {key: {"attempt": 8, "result": "published"}},
        }), encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "verify_publisher_state.py"

        result = subprocess.run(
            [sys.executable, str(script), str(approved), str(state_file)],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_attempt_does_not_create_publication(self):
        with self.assertRaises(RuntimeError):
            publisher.publish_story("Unpublished", "Not yet")

        self.assertFalse(publisher.PUBLISH_DIR.exists())
        self.assertEqual(list(publisher._load_state()["attempts"].values()), [1])

    def test_approval_events_are_revision_scoped(self):
        from app.approval import approval_event_name

        self.assertEqual(approval_event_name(0), "approval-0")
        self.assertEqual(approval_event_name(1), "approval-1")


if __name__ == "__main__":
    unittest.main()

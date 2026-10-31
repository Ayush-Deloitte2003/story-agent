import importlib
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
                lambda _: publisher.publish_story("Concurrent", "One approved draft"),
                range(8),
            ))

        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(list(publisher.PUBLISH_DIR.glob("*.md"))), 1)
        state = publisher._load_state()
        self.assertEqual(list(state["attempts"].values()), [1])

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

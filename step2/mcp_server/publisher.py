"""A flaky "publishing service" exposed as an MCP tool.

Each story fails its first MCP_FAIL_TIMES publish attempts with a
RuntimeError, then succeeds. Attempts are counted per story (title + text)
in a JSON file, so the count survives restarts of this server.

    python mcp_server/publisher.py          # serves http://127.0.0.1:8765/mcp

Settings (environment or .env):
    MCP_FAIL_TIMES   failures before success, per story (default 7)
    MCP_PORT         port to listen on (default 8765)
    MCP_STATE_FILE   where attempt counts are kept (default mcp_server/attempts.json)
"""

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

FAIL_TIMES = int(os.getenv("MCP_FAIL_TIMES", "7"))
PORT = int(os.getenv("MCP_PORT", "8765"))
STATE_FILE = Path(os.getenv("MCP_STATE_FILE", BASE_DIR / "mcp_server" / "attempts.json"))
PUBLISH_DIR = Path(os.getenv("PUBLISH_DIR", BASE_DIR / "published"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [mcp] %(message)s")
log = logging.getLogger("publisher")
logging.getLogger("mcp").setLevel(logging.WARNING)
_lock = threading.Lock()

mcp = FastMCP("publisher", host="127.0.0.1", port=PORT, log_level="WARNING")


def _next_attempt(key: str) -> int:
    with _lock:
        counts = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        counts[key] = counts.get(key, 0) + 1
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(counts, indent=2))
        return counts[key]


@mcp.tool()
def publish_story(title: str, story: str) -> str:
    """Publish a finished story. A human must approve this call before it runs.

    Args:
        title: Short title for the story.
        story: The complete story text.
    """
    key = hashlib.sha256(f"{title}\n{story}".encode()).hexdigest()[:12]
    attempt = _next_attempt(key)

    if attempt <= FAIL_TIMES:
        log.info("publish '%s' attempt %d of %d: FAIL", title, attempt, FAIL_TIMES + 1)
        raise RuntimeError(
            f"Publishing service unavailable (attempt {attempt}; "
            f"this story succeeds on attempt {FAIL_TIMES + 1})"
        )

    PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "story"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = PUBLISH_DIR / f"{stamp}-{slug}.md"
    path.write_text(f"# {title}\n\n{story}\n", encoding="utf-8")
    log.info("publish '%s' attempt %d: OK -> %s", title, attempt, path.name)
    return f"Published '{title}' to published/{path.name} on attempt {attempt}"


if __name__ == "__main__":
    log.info("Publisher MCP server on http://127.0.0.1:%d/mcp (fails first %d attempts per story)", PORT, FAIL_TIMES)
    mcp.run(transport="streamable-http")

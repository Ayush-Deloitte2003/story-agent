"""A flaky "publishing service" exposed as an MCP tool.

Each story fails its first MCP_FAIL_TIMES publish attempts with a
RuntimeError, then succeeds. Attempts are counted per story (title + text)
in a JSON file, so the count survives restarts of this server.

    python mcp_server/publisher.py          # serves http://127.0.0.1:8765/mcp

Settings (environment or .env):
    MCP_FAIL_TIMES   failures before success, per story (default 7)
    MCP_HOST         address to listen on (default 127.0.0.1)
    MCP_PORT         port to listen on (default 8765)
    PUBLISH_DIR      where published stories go (default ./published)
    MCP_STATE_FILE   where attempt counts are kept (default mcp_server/attempts.json)
"""

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

FAIL_TIMES = int(os.getenv("MCP_FAIL_TIMES", "7"))
HOST = os.getenv("MCP_HOST", "127.0.0.1")  # 0.0.0.0 inside Kubernetes
PORT = int(os.getenv("MCP_PORT", "8765"))
STATE_FILE = Path(os.getenv("MCP_STATE_FILE", BASE_DIR / "mcp_server" / "attempts.json"))
PUBLISH_DIR = Path(os.getenv("PUBLISH_DIR", BASE_DIR / "published"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [mcp] %(message)s")
log = logging.getLogger("publisher")
logging.getLogger("mcp").setLevel(logging.WARNING)
_lock = threading.Lock()

mcp = FastMCP("publisher", host=HOST, port=PORT, log_level="WARNING")


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"attempts": {}, "published": {}}
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if "published" not in state:
        state = {"attempts": state, "published": {}}
    return state


def _write_atomic(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _publish_once(key: str, title: str, story: str) -> str:
    with _lock:
        state = _load_state()
        previous = state["published"].get(key)
        if previous:
            return previous["result"]

        attempt = state["attempts"].get(key, 0) + 1
        state["attempts"][key] = attempt
        _write_atomic(STATE_FILE, json.dumps(state, indent=2))

        if attempt <= FAIL_TIMES:
            log.info("publish '%s' attempt %d of %d: FAIL", title, attempt, FAIL_TIMES + 1)
            raise RuntimeError(
                f"Publishing service unavailable (attempt {attempt}; "
                f"this story succeeds on attempt {FAIL_TIMES + 1})"
            )

        PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "story"
        path = PUBLISH_DIR / f"{slug}-{key}.md"
        _write_atomic(path, f"# {title}\n\n{story}\n")
        result = f"Published '{title}' to published/{path.name} on attempt {attempt}"
        state["published"][key] = {"attempt": attempt, "result": result}
        _write_atomic(STATE_FILE, json.dumps(state, indent=2))
        log.info("publish '%s' attempt %d: OK -> %s", title, attempt, path.name)
        return result


@mcp.tool()
def publish_story(title: str, story: str) -> str:
    """Publish a finished story. A human must approve this call before it runs.

    Args:
        title: Short title for the story.
        story: The complete story text.
    """
    key = hashlib.sha256(f"{title}\n{story}".encode()).hexdigest()
    return _publish_once(key, title, story)


if __name__ == "__main__":
    log.info("Publisher MCP server on http://%s:%d/mcp (fails first %d attempts per story)", HOST, PORT, FAIL_TIMES)
    mcp.run(transport="streamable-http")

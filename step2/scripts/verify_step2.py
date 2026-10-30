"""Check Step 2 (and that Step 1 still works), and write a PASS/FAIL report.

    python scripts/verify_step2.py fake   # scripted stand-in model, no API key needed
    python scripts/verify_step2.py real   # your OpenAI key from .env

Starts its own two publisher MCP servers, so nothing else needs to be running:
  - a healthy one (fails 0 times) to prove the agent publishes through MCP
  - a flaky one (fails MCP_FAIL_TIMES times, default 7) to show the failure

Exit code: 0 all passed, 1 something failed, 2 skipped (no key for real mode).
"""

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

MODE = sys.argv[1] if len(sys.argv) > 1 else "fake"
REPORT = ROOT / f"verify_output_step2_{MODE}.txt"
lines: list[str] = []
failures = 0


def out(text: str = "") -> None:
    print(text, flush=True)
    lines.append(text)


def check(name: str, ok: bool, detail: str = "") -> bool:
    global failures
    failures += 0 if ok else 1
    out(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def save_and_exit(code: int) -> None:
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReport saved to {REPORT}")
    sys.exit(code)


# ---------------------------------------------------------------- model setup
if MODE == "fake":
    os.environ["OPENAI_API_KEY"] = "not-used"
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    import app.agent as agent_module

    class ScriptedModel(BaseChatModel):
        """Behaves like a well-behaved LLM: load skill, publish, confirm."""

        @property
        def _llm_type(self) -> str:
            return "scripted"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            last = messages[-1]
            n = sum(1 for m in messages if m.type == "tool")

            def call(name, args):
                return AIMessage("", tool_calls=[{"id": f"c{n}", "name": name, "args": args, "type": "tool_call"}])

            if n == 0:
                msg = call("read_file", {"file_path": "/hp-season-1/SKILL.md"})
            elif last.type == "tool" and last.name == "read_file":
                stamp = time.time_ns()  # a fresh story each run, so attempt counts start at 1
                msg = call("publish_story", {"title": "The Boy Under the Stairs", "story": f"Draft {stamp}."})
            else:
                msg = AIMessage(f"Done. {text(last.content)}")
            return ChatResult(generations=[ChatGeneration(message=msg)])

    agent_module.ChatLiteLLM = lambda **kw: ScriptedModel()
else:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    key = os.getenv("OPENAI_API_KEY", "")
    if not key or key.startswith("sk-..."):
        out("SKIPPED real-model check: no OPENAI_API_KEY in .env")
        save_and_exit(2)

from fastapi.testclient import TestClient  # noqa: E402
from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: E402

import app.main as main  # noqa: E402

FAIL_TIMES = int(os.getenv("MCP_FAIL_TIMES", "7"))
published = ROOT / "published"
tmp = Path(tempfile.mkdtemp(prefix="story-agent-verify-"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_publisher(fail_times: int) -> tuple[subprocess.Popen, str]:
    port = free_port()
    env = dict(os.environ, MCP_PORT=str(port), MCP_FAIL_TIMES=str(fail_times),
               MCP_STATE_FILE=str(tmp / f"attempts-{port}.json"))
    log = open(tmp / f"publisher-{port}.log", "w")
    proc = subprocess.Popen([sys.executable, str(ROOT / "mcp_server" / "publisher.py")],
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 30
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, f"http://127.0.0.1:{port}/mcp"
        if proc.poll() is not None:
            break
        time.sleep(0.3)
    raise RuntimeError(f"Publisher MCP server did not start; see {log.name}")


def text(content) -> str:
    """Plain text from a message or tool result that may be a list of blocks."""
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def files() -> set[Path]:
    return set(published.glob("*.md")) if published.exists() else set()


def write_story(client) -> dict:
    r = client.post("/chat", json={"message": "Write the season 1 story."})
    return r.json() if r.status_code == 200 else {"status": f"HTTP {r.status_code}", "detail": r.text[:500]}


healthy, healthy_url = start_publisher(0)
flaky, flaky_url = start_publisher(FAIL_TIMES)
created: set[Path] = set()

try:
    out(f"Step 2 verification, mode={MODE}, model={os.getenv('LLM_MODEL', 'openai/gpt-4o') if MODE == 'real' else 'scripted stand-in'}")
    out(f"Flaky publisher fails the first {FAIL_TIMES} attempts per story")
    out("")

    # ------------------------------------------ Part A: healthy MCP server
    out("Part A: publishing through a healthy MCP server (Step 1 flow + MCP)")
    os.environ["MCP_URL"] = healthy_url
    before = files()
    with TestClient(main.app) as client:
        names = [t.name for t in asyncio.run(main.load_mcp_tools())]
        check("Agent loads publish_story from the MCP server", "publish_story" in names, ", ".join(names))
        data = write_story(client)
        tid = data.get("thread_id")
        state = main.agent.get_state({"configurable": {"thread_id": tid}}) if tid else None
        read_skill = bool(state) and any(
            m.type == "tool" and m.name == "read_file" and "hp-season-1" in str(m.content)
            for m in state.values.get("messages", [])
        )
        check("Agent invoked the skill", read_skill)
        check("Run paused for human approval", data.get("status") == "awaiting_approval", f"status={data.get('status')}")
        story = (data.get("story") or {}).get("story", "")
        check("LLM wrote a story", bool(story), f"{len(story.split())} words")
        check("Nothing published before approval", files() == before)
        r = client.post(f"/chat/{tid}/decision", json={"decision": "approve"})
        d = r.json()
        check("Approve publishes through MCP", d.get("status") == "completed", f"status={d.get('status')}")
        new = files() - before
        created |= new
        check("Story file written by the MCP server", len(new) == 1, ", ".join(p.name for p in new))
        out(f"Agent's final reply: {d.get('reply')}")
    out("")

    # ------------------------------------------ Part B: flaky MCP server
    out(f"Part B: publishing through the flaky MCP server (fails {FAIL_TIMES} times)")
    os.environ["MCP_URL"] = flaky_url
    before = files()
    with TestClient(main.app) as client:
        data = write_story(client)
        tid = data.get("thread_id")
        check("Story written and paused for approval", data.get("status") == "awaiting_approval", f"status={data.get('status')}")
        r = client.post(f"/chat/{tid}/decision", json={"decision": "approve"})
        d = r.json()
        check("After approval, the MCP tool's RuntimeError stops the run",
              d.get("status") == "failed" and "Publishing service unavailable" in d.get("error", ""),
              f"status={d.get('status')}")
        out(f"    error: {d.get('error')}")
        check("Nothing was published", files() == before)
        r = client.get(f"/chat/{tid}")
        check("Status endpoint reports the failure", r.json().get("status") == "failed")
        r = client.post(f"/chat/{tid}/decision", json={"decision": "approve"})
        check("The approval cannot be reused: the human would have to start over", r.status_code == 409, f"HTTP {r.status_code}")
    out("")

    # ------------------------------------------ Part C: the retry contract
    out(f"Part C: calling the flaky tool directly {FAIL_TIMES + 1} times (what Step 3 must automate)")
    client = MultiServerMCPClient({"p": {"transport": "streamable_http", "url": flaky_url}}, handle_tool_errors=False)

    async def hammer():
        tool = next(t for t in await client.get_tools() if t.name == "publish_story")
        results = []
        for _ in range(FAIL_TIMES + 1):
            try:
                results.append(("ok", text(await tool.ainvoke({"title": "Retry test", "story": "Same story every time."}))))
            except Exception as exc:  # noqa: BLE001
                results.append(("fail", str(exc)))
        return results

    before = files()
    results = asyncio.run(hammer())
    for i, (kind, msg) in enumerate(results, 1):
        out(f"    call {i}: {kind.upper():4} {msg[:90]}")
    check(f"First {FAIL_TIMES} calls raise RuntimeError", all(k == "fail" for k, _ in results[:-1]))
    check(f"Call {FAIL_TIMES + 1} succeeds and publishes", results[-1][0] == "ok" and len(files() - before) == 1)
    retry_files = files() - before
finally:
    healthy.terminate()
    flaky.terminate()
    # Test-only output: Part C's file always, Part A's when the story is a stand-in.
    for p in (retry_files if "retry_files" in dir() else set()) | (created if MODE == "fake" else set()):
        p.unlink(missing_ok=True)

out("")
out("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
save_and_exit(0 if failures == 0 else 1)

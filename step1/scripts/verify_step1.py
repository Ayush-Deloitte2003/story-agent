"""Check that every part of Step 1 works, and write a PASS/FAIL report.

    python scripts/verify_step1.py fake   # scripted stand-in model, no API key needed
    python scripts/verify_step1.py real   # your OpenAI key from .env

Exit code: 0 all passed, 1 something failed, 2 skipped (no key for real mode).
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

MODE = sys.argv[1] if len(sys.argv) > 1 else "fake"
REPORT = ROOT / f"verify_output_{MODE}.txt"
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


if MODE == "fake":
    os.environ["OPENAI_API_KEY"] = "not-used"
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    import app.agent as agent_module

    class ScriptedModel(BaseChatModel):
        """Behaves like a well-behaved LLM: load skill, publish, revise, confirm."""

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
                msg = call("publish_story", {"title": "The Boy Under the Stairs", "story": "Draft one."})
            elif last.type == "tool" and "Published" in str(last.content):
                msg = AIMessage(f"Done. {last.content}")
            else:
                msg = call("publish_story", {"title": "The Boy Under the Stairs", "story": "Draft two, revised after feedback."})
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

import app.main as main  # noqa: E402

client = TestClient(main.app)
published = ROOT / "published"
before = set(published.glob("*.md")) if published.exists() else set()

out(f"Step 1 verification, mode={MODE}, model={os.getenv('LLM_MODEL', 'openai/gpt-4o') if MODE == 'real' else 'scripted stand-in'}")
out(f"Run at {datetime.now().isoformat(timespec='seconds')}")
out("")

# 1. FastAPI /chat
r = client.post("/chat", json={"message": "Write the season 1 story."})
if not check("FastAPI POST /chat responds", r.status_code == 200, f"HTTP {r.status_code}"):
    out(r.text[:1000])
    save_and_exit(1)
data = r.json()
tid = data["thread_id"]

# 2. Skill invocation
state = main.agent.get_state({"configurable": {"thread_id": tid}})
skill_reads = [
    m for m in state.values["messages"]
    if m.type == "tool" and m.name == "read_file" and "hp-season-1" in str(m.content)
]
check("Agent invoked the skill (read hp-season-1/SKILL.md)", bool(skill_reads))

# 3. LLM wrote a story and 4. run paused for approval
check("Run paused for human approval", data.get("status") == "awaiting_approval", f"status={data.get('status')}")
story = data.get("story") or {}
check("LLM wrote a story", bool(story.get("story")), f"{len(str(story.get('story', '')).split())} words")
after_pause = set(published.glob("*.md")) if published.exists() else set()
check("Nothing published before approval", after_pause == before)
check("LLM calls logged for the request", data.get("llm_calls", 0) >= 1, f"llm_calls={data.get('llm_calls')}")

expires = datetime.fromisoformat(data["approval_expires_at"]) if "approval_expires_at" in data else None
hours = (expires - datetime.now(timezone.utc)).total_seconds() / 3600 if expires else 0
check("Approval window set", 23 < hours <= float(os.getenv("APPROVAL_TIMEOUT_HOURS", "24")), f"{hours:.1f} hours left")

out("")
out(f"--- Story shown for approval: {story.get('title')} ---")
out(str(story.get("story", "")))
out("---")
out("")

# 5. Reject with feedback -> revised draft
r = client.post(f"/chat/{tid}/decision", json={"decision": "reject", "feedback": "Make it a little funnier."})
d2 = r.json()
check("Reject with feedback sends it back for revision", r.status_code == 200 and d2.get("status") == "awaiting_approval", f"status={d2.get('status')}")
revised = (d2.get("story") or {}).get("story", "")
check("Revised draft differs from the first", bool(revised) and revised != story.get("story"))
if MODE == "real":
    out("")
    out("--- Revised story ---")
    out(revised)
    out("---")
    out("")

# 6. Approve -> published
r = client.post(f"/chat/{tid}/decision", json={"decision": "approve"})
d3 = r.json()
check("Approve completes the run", r.status_code == 200 and d3.get("status") == "completed", f"status={d3.get('status')}")
new_files = (set(published.glob("*.md")) if published.exists() else set()) - before
check("Story file written to published/", len(new_files) == 1, ", ".join(p.name for p in new_files))
out(f"Agent's final reply: {d3.get('reply')}")

# 7. Guard rails
r = client.post(f"/chat/{tid}/decision", json={"decision": "approve"})
check("A second approval is refused", r.status_code == 409, f"HTTP {r.status_code}")
r = client.get("/chat/does-not-exist")
check("Unknown thread returns 404", r.status_code == 404, f"HTTP {r.status_code}")

if MODE == "fake":
    # 8. Expiry: start a run, push its deadline into the past, try to approve.
    r = client.post("/chat", json={"message": "Write the season 1 story."})
    tid2 = r.json()["thread_id"]
    main.approval_deadlines[tid2] = datetime.now(timezone.utc) - timedelta(seconds=1)
    r = client.post(f"/chat/{tid2}/decision", json={"decision": "approve"})
    check("Approval after the window expires is refused", r.status_code == 410, f"HTTP {r.status_code}")
    for p in (set(published.glob("*.md")) - before):
        p.unlink()  # stand-in stories are not real output

out("")
out("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
save_and_exit(0 if failures == 0 else 1)

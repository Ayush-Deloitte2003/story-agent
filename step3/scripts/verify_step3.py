"""Check Step 3 against the running app, and write a PASS/FAIL report.

The app must already be running under Dapr (locally with `dapr run`, or in
Kind with `kubectl port-forward svc/story-agent 8000:8000`).

    python scripts/verify_step3.py            # full flow: revise, retries, expiry
    python scripts/verify_step3.py start      # durability, part 1: leave a story awaiting approval
    (now stop and restart the app, or delete its pod in Kind)
    python scripts/verify_step3.py resume     # durability, part 2: approve it after the restart

Options: --url http://127.0.0.1:8000
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent

parser = argparse.ArgumentParser()
parser.add_argument(
    "mode", nargs="?", default="run",
    choices=[
        "run", "start", "resume", "publishing-start", "publishing-resume", "redis-start",
        "redis-resume", "expiry-start", "expiry-resume", "exhaust",
    ],
)
parser.add_argument("--url", default="http://127.0.0.1:8000")
args = parser.parse_args()
report_prefix = os.getenv("VERIFY_REPORT_PREFIX")
report_name = f"verify_output_step3_{report_prefix}_{args.mode}.txt" if report_prefix else f"verify_output_step3_{args.mode}.txt"
REPORT = ROOT / report_name
APPROVED_DRAFT = ROOT / f"verify_output_step3_{args.mode}_approved.json"
if args.mode.startswith("expiry-"):
    pending_name = ".step3_pending_expiry.json"
elif args.mode.startswith("publishing-"):
    pending_name = ".step3_pending_publishing.json"
else:
    pending_name = ".step3_pending.json"
PENDING = ROOT / pending_name

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


def finish() -> None:
    out("")
    out("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReport saved to {REPORT}")
    sys.exit(0 if failures == 0 else 1)


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(args.url + path, data=data, method=method, headers={"content-type": "application/json"})
    try:
        with request.urlopen(req, timeout=300) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_until_done(tid: str, limit: float = 300) -> dict:
    deadline = time.time() + limit
    while True:
        _, view = call("GET", f"/chat/{tid}")
        if view.get("status") not in ("publishing", "writing") or time.time() > deadline:
            return view
        time.sleep(2)


def attempt_of(view: dict) -> int | None:
    match = re.search(r"on attempt (\d+)", str(view.get("reply", "")))
    return int(match.group(1)) if match else None


try:
    call("GET", "/chat/ping")
except error.URLError:
    print(f"Cannot reach the app at {args.url}. Start it first (see README).")
    sys.exit(1)

out(f"Step 3 verification ({args.mode}) against {args.url}")
out("")

if args.mode == "run":
    out("Part A: write, revise, approve; Dapr retries the flaky MCP tool")
    code, v = call("POST", "/chat", {"message": "Write the season 1 story."})
    tid = v.get("thread_id")
    check("POST /chat starts a Dapr workflow and pauses for approval",
          code == 200 and v.get("status") == "awaiting_approval", f"HTTP {code}, status={v.get('status')}")
    story = (v.get("story") or {}).get("story", "")
    check("Deep agent wrote a story", bool(story), f"{len(story.split())} words, llm_calls={v.get('llm_calls')}")
    check("Approval window is set", bool(v.get("approval_expires_at")), str(v.get("approval_expires_at")))

    code, v2 = call("POST", f"/chat/{tid}/decision", {
        "decision": "reject", "feedback": "Make it a little funnier.", "expected_revision": 0
    })
    revised = (v2.get("story") or {}).get("story", "")
    check("Reject with feedback produces a revised draft",
          v2.get("status") == "awaiting_approval" and v2.get("revision") == 1 and revised != story,
          f"status={v2.get('status')}, revision={v2.get('revision')}")
    code, stale = call("POST", f"/chat/{tid}/decision", {
        "decision": "approve", "expected_revision": 0
    })
    check("A delayed decision for the prior revision is refused",
          code == 409 and stale.get("detail") == "This draft has changed. Refresh before deciding.",
          f"HTTP {code}, status={stale.get('detail')}")

    started = time.time()
    code, v3 = call("POST", f"/chat/{tid}/decision", {"decision": "approve", "expected_revision": 1})
    check("Approve hands publishing to Dapr", v3.get("status") in ("publishing", "published"), f"status={v3.get('status')}")
    out("    waiting for Dapr to retry the MCP tool...")
    done = wait_until_done(tid)
    attempt = attempt_of(done)
    check("Story is published after Dapr's retries", done.get("status") == "published",
          f"status={done.get('status')}, {time.time() - started:.0f}s")
    check("The MCP tool succeeded on exactly attempt 8", attempt == 8, f"succeeded on attempt {attempt}")
    if done.get("status") == "published" and isinstance(done.get("story"), dict):
        APPROVED_DRAFT.write_text(
            json.dumps({"title": done["story"]["title"], "story": done["story"]["story"], "reply": done.get("reply")}),
            encoding="utf-8",
        )
        out(f"    approved draft evidence saved to {APPROVED_DRAFT.name}")
    out(f"    {done.get('reply') or done.get('error')}")
    code, _ = call("POST", f"/chat/{tid}/decision", {"decision": "approve"})
    check("A second approval is refused", code == 409, f"HTTP {code}")
    out("")

    out("Part B: a configurable approval window that expires")
    code, v = call("POST", "/chat", {"message": "Write the season 1 story.", "approval_timeout_seconds": 10})
    tid = v.get("thread_id")
    check("Story waiting with a 10-second window", v.get("status") == "awaiting_approval", str(v.get("approval_expires_at")))
    time.sleep(15)
    _, v = call("GET", f"/chat/{tid}")
    check("Dapr's timer ended the workflow as expired", v.get("status") == "expired", f"status={v.get('status')}")
    code, _ = call("POST", f"/chat/{tid}/decision", {"decision": "approve"})
    check("Approving after expiry is refused", code == 410, f"HTTP {code}")
    code, _ = call("GET", "/chat/does-not-exist")
    check("Unknown thread returns 404", code == 404, f"HTTP {code}")

elif args.mode in ("start", "redis-start"):
    code, v = call("POST", "/chat", {"message": "Write the season 1 story."})
    ok = check("Story is waiting for approval", v.get("status") == "awaiting_approval", f"thread {v.get('thread_id')}")
    if ok:
        PENDING.write_text(json.dumps({"thread_id": v["thread_id"], "title": v["story"]["title"]}))
        out("")
        out("Now stop the app (Ctrl+C in its terminal, or `kubectl delete pod -l app=story-agent` in Kind),")
        out("start it again, then run:  python scripts/verify_step3.py resume")

elif args.mode == "expiry-start":
    code, v = call("POST", "/chat", {
        "message": "Write the season 1 story.", "approval_timeout_seconds": 10
    })
    ok = check("Short-lived story is waiting for approval",
               v.get("status") == "awaiting_approval", f"thread {v.get('thread_id')}")
    if ok:
        PENDING.write_text(json.dumps({"thread_id": v["thread_id"], "title": v["story"]["title"]}))
        out("Stop the app longer than the 10-second approval window, restart it, then run expiry-resume.")

elif args.mode == "publishing-start":
    code, draft = call("POST", "/chat", {"message": "Write a brief season 1 story about a new student."})
    tid = draft.get("thread_id")
    ok = check("Publishing-recovery workflow waits for approval",
               code == 200 and draft.get("status") == "awaiting_approval", f"HTTP {code}, status={draft.get('status')}")
    if ok:
        code, publishing = call("POST", f"/chat/{tid}/decision", {
            "decision": "approve", "expected_revision": draft.get("revision", 0)
        })
        check("Approval starts publishing before app replacement",
              code == 200 and publishing.get("status") in ("publishing", "published"),
              f"HTTP {code}, status={publishing.get('status')}")
        PENDING.write_text(json.dumps({"thread_id": tid, "title": draft["story"]["title"]}))
        APPROVED_DRAFT.write_text(
            json.dumps({
                "title": draft["story"]["title"],
                "story": draft["story"]["story"],
                "reply": publishing.get("reply"),
            }),
            encoding="utf-8",
        )

elif args.mode in ("resume", "redis-resume"):
    if not PENDING.exists():
        print("Run `verify_step3.py start` first.")
        sys.exit(1)
    pending = json.loads(PENDING.read_text())
    tid = pending["thread_id"]
    code, v = call("GET", f"/chat/{tid}")
    check("After the restart, the story is still awaiting approval",
          v.get("status") == "awaiting_approval" and (v.get("story") or {}).get("title") == pending["title"],
          f"status={v.get('status')}, title={(v.get('story') or {}).get('title')}")
    call("POST", f"/chat/{tid}/decision", {
        "decision": "approve", "expected_revision": v.get("revision", 0)
    })
    out("    approved; waiting for Dapr to retry the MCP tool...")
    done = wait_until_done(tid)
    check("Approved after the restart and published", done.get("status") == "published",
          f"attempt {attempt_of(done)}")
    out(f"    {done.get('reply') or done.get('error')}")
    PENDING.unlink(missing_ok=True)

elif args.mode == "publishing-resume":
    if not PENDING.exists():
        print("Run `verify_step3.py publishing-start` first.")
        sys.exit(1)
    pending = json.loads(PENDING.read_text())
    tid = pending["thread_id"]
    _, view = call("GET", f"/chat/{tid}")
    check("Workflow recovered in publishing after app and publisher replacement",
          view.get("status") in ("publishing", "published"), f"status={view.get('status')}")
    done = wait_until_done(tid)
    attempt = attempt_of(done)
    check("Recovered publish succeeds on attempt 8",
          done.get("status") == "published" and attempt == 8,
          f"status={done.get('status')}, attempt={attempt}")
    if done.get("status") == "published":
        approved = json.loads(APPROVED_DRAFT.read_text(encoding="utf-8"))
        approved["reply"] = done.get("reply")
        APPROVED_DRAFT.write_text(json.dumps(approved), encoding="utf-8")
    out(f"    {done.get('reply') or done.get('error')}")
    PENDING.unlink(missing_ok=True)

elif args.mode == "expiry-resume":
    if not PENDING.exists():
        print("Run `verify_step3.py expiry-start` first.")
        sys.exit(1)
    pending = json.loads(PENDING.read_text())
    tid = pending["thread_id"]
    _, view = call("GET", f"/chat/{tid}")
    check("Approval expired while the app was unavailable",
          view.get("status") == "expired", f"status={view.get('status')}")
    code, _ = call("POST", f"/chat/{tid}/decision", {"decision": "approve"})
    check("Approving after downtime expiry is refused", code == 410, f"HTTP {code}")
    PENDING.unlink(missing_ok=True)

else:  # exhaust
    code, draft = call("POST", "/chat", {"message": "Write a brief season 1 story about a new student."})
    tid = draft.get("thread_id")
    check("Retry-exhaustion workflow waits for approval",
          code == 200 and draft.get("status") == "awaiting_approval", f"HTTP {code}, status={draft.get('status')}")
    if draft.get("status") == "awaiting_approval":
        call("POST", f"/chat/{tid}/decision", {
            "decision": "approve", "expected_revision": draft.get("revision", 0)
        })
        done = wait_until_done(tid)
        check("Workflow reaches failed after the configured retry limit",
              done.get("status") == "failed", f"status={done.get('status')}, error={done.get('error')}")

finish()

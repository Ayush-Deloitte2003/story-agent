"""Verify a specialist Dapr workflow recovers after app replacement and event replay."""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.events import (  # noqa: E402
    workflow_id_for_brief_compliance_event,
    workflow_id_for_continuity_check_event,
)


APP_URL = os.getenv("APP_URL", "http://127.0.0.1:8000").rstrip("/")
STORY = (
    "Mara found a brass key in the school garden. She returned it to the "
    "librarian, who used it to open the long-locked seed cabinet."
)
REQUEST = "Write a short story about a child helping at a community garden."
SPECIALISTS = {
    "continuity-check": {
        "webhook": "story-continuity-check-requested",
        "status_path": "continuity-check",
        "stage": "checking_continuity",
        "workflow_id": workflow_id_for_continuity_check_event,
        "validate": lambda result: (
            isinstance(result.get("consistent"), bool)
            and isinstance(result.get("summary"), str)
            and isinstance(result.get("findings"), list)
        ),
    },
    "brief-compliance": {
        "webhook": "story-brief-compliance-requested",
        "status_path": "brief-compliance",
        "stage": "checking_brief_compliance",
        "workflow_id": workflow_id_for_brief_compliance_event,
        "validate": lambda result: (
            isinstance(result.get("compliant"), bool)
            and isinstance(result.get("summary"), str)
            and isinstance(result.get("requirements"), list)
        ),
    },
}


def request_json(url, payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(
            f"{request.method} {url} returned HTTP {exc.code}: "
            f"{exc.read().decode('utf-8', errors='replace')}"
        ) from exc
    return json.loads(content) if content else None


def pending_path(name):
    return ROOT / f".step3_{name.replace('-', '_')}_recovery.json"


def status_url(name, event):
    source = urllib.parse.urlencode({"source": event["source"]})
    event_id = urllib.parse.quote(event["id"])
    return f"{APP_URL}/events/{SPECIALISTS[name]['status_path']}/{event_id}?{source}"


def start(name):
    event = {
        "id": str(uuid.uuid4()),
        "source": f"step3-{name}-recovery",
        "title": "The Seed Cabinet Key",
        "story": STORY,
        "request": REQUEST,
    }
    pending_path(name).write_text(json.dumps(event), encoding="utf-8")
    accepted = request_json(f"{APP_URL}/webhooks/{SPECIALISTS[name]['webhook']}", event)
    if accepted.get("status") != "ACCEPTED":
        raise AssertionError(f"{name} webhook did not accept event: {accepted}")

    expected_id = SPECIALISTS[name]["workflow_id"](event["source"], event["id"])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = request_json(status_url(name, event))
        if state is not None:
            if state.get("thread_id") != expected_id:
                raise AssertionError(f"Unexpected {name} workflow ID: {state}")
            print(f"[PASS] {name} scheduled durable workflow {expected_id}")
            print(f"    status before app replacement: {state.get('status')}")
            return
        time.sleep(1)
    raise TimeoutError(f"{name} workflow state did not appear before app replacement.")


def resume(name):
    path = pending_path(name)
    if not path.exists():
        raise FileNotFoundError(f"Run verify_specialist_recovery.py {name} start first.")
    event = json.loads(path.read_text(encoding="utf-8"))
    expected_id = SPECIALISTS[name]["workflow_id"](event["source"], event["id"])

    accepted = request_json(f"{APP_URL}/webhooks/{SPECIALISTS[name]['webhook']}", event)
    if accepted.get("status") != "ACCEPTED":
        raise AssertionError(f"Replay of {name} event was not accepted: {accepted}")

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = request_json(status_url(name, event))
        if state is not None and state.get("status") in ("reviewed", "failed"):
            break
        time.sleep(2)
    else:
        raise TimeoutError(f"{name} workflow did not complete after app replacement.")

    if state.get("thread_id") != expected_id:
        raise AssertionError(f"Replayed event did not resolve to the original workflow: {state}")
    if state.get("status") != "reviewed":
        raise RuntimeError(f"{name} workflow failed after app replacement: {state}")
    result = state.get("review")
    if not isinstance(result, dict) or not SPECIALISTS[name]["validate"](result):
        raise AssertionError(f"Recovered {name} workflow returned an invalid result: {state}")

    print(f"[PASS] Same event ID resolved after app replacement to {expected_id}")
    print(f"[PASS] Recovered {name} workflow returned its validated advisory result")
    path.unlink()


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in SPECIALISTS or sys.argv[2] not in ("start", "resume"):
        raise SystemExit(
            "Usage: verify_specialist_recovery.py {continuity-check|brief-compliance} {start|resume}"
        )
    name, mode = sys.argv[1:]
    (start if mode == "start" else resume)(name)


if __name__ == "__main__":
    main()

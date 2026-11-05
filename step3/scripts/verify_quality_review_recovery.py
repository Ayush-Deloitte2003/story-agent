"""Verify a quality-review workflow survives app replacement and event replay."""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.events import workflow_id_for_quality_review_event


APP_URL = os.getenv("APP_URL", "http://127.0.0.1:8000").rstrip("/")
PENDING = ROOT / ".step3_quality_review_pending.json"


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
        raise RuntimeError(f"{request.method} {url} returned HTTP {exc.code}: {exc.read().decode()}") from exc
    return json.loads(content) if content else None


def status_url(pending):
    source = urllib.parse.urlencode({"source": pending["source"]})
    event_id = urllib.parse.quote(pending["id"])
    return f"{APP_URL}/events/quality-review/{event_id}?{source}"


def start():
    event = {
        "id": str(uuid.uuid4()),
        "source": "step3-quality-review-recovery",
        "title": "The Paper Bridge",
        "story": (
            "When rain cut off the footpath, Jo folded a paper bridge for a toy "
            "boat, then helped the neighbors build a real path from spare boards."
        ),
        "request": "Write a short story about practical kindness.",
    }
    PENDING.write_text(json.dumps(event), encoding="utf-8")
    accepted = request_json(f"{APP_URL}/webhooks/story-quality-review-requested", event)
    if accepted.get("status") != "ACCEPTED":
        raise AssertionError(f"Quality-review webhook did not accept the event: {accepted}")

    expected_id = workflow_id_for_quality_review_event(event["source"], event["id"])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = request_json(status_url(event))
        if state is not None:
            if state.get("thread_id") != expected_id:
                raise AssertionError(f"Unexpected quality-review workflow ID: {state}")
            print(f"[PASS] Quality-review event scheduled as durable workflow {expected_id}")
            print(f"    status before app replacement: {state.get('status')}")
            return
        time.sleep(1)
    raise TimeoutError("Quality-review workflow state did not appear before app replacement.")


def resume():
    if not PENDING.exists():
        raise FileNotFoundError("Run verify_quality_review_recovery.py start first.")
    event = json.loads(PENDING.read_text(encoding="utf-8"))
    expected_id = workflow_id_for_quality_review_event(event["source"], event["id"])

    accepted = request_json(f"{APP_URL}/webhooks/story-quality-review-requested", event)
    if accepted.get("status") != "ACCEPTED":
        raise AssertionError(f"Replay of the quality-review event was not accepted: {accepted}")

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = request_json(status_url(event))
        if state is not None and state.get("status") in ("reviewed", "failed"):
            break
        time.sleep(2)
    else:
        raise TimeoutError("Quality-review workflow did not complete after app replacement.")

    if state.get("thread_id") != expected_id:
        raise AssertionError(f"Replayed event did not resolve to the original workflow: {state}")
    if state.get("status") != "reviewed":
        raise RuntimeError(f"Quality-review workflow failed after app replacement: {state}")
    review = state.get("review")
    if not isinstance(review, dict) or review.get("recommendation") not in ("ready", "revise"):
        raise AssertionError(f"Recovered workflow has no valid review result: {state}")

    print(f"[PASS] Same event ID resolved after app replacement to {expected_id}")
    print("[PASS] Recovered workflow returned its completed advisory review")
    PENDING.unlink()


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2 or sys.argv[1] not in ("start", "resume"):
        raise SystemExit("Usage: verify_quality_review_recovery.py {start|resume}")
    if sys.argv[1] == "start":
        start()
    else:
        resume()

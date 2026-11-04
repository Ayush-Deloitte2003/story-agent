"""Publish a CloudEvent through Dapr Pub/Sub and verify its workflow reaches review."""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


DAPR_URL = os.getenv("DAPR_HTTP_URL", "http://127.0.0.1:3500").rstrip("/")
APP_URL = os.getenv("APP_URL", "http://127.0.0.1:8000").rstrip("/")


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


def publish_event(event):
    publish_url = f"{DAPR_URL}/v1.0/publish/pubsub/story-events"
    request = urllib.request.Request(
        publish_url,
        data=json.dumps(event).encode("utf-8"),
        headers={"Content-Type": "application/cloudevents+json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status not in (200, 202, 204):
            raise RuntimeError(f"Publishing event returned HTTP {response.status}")


def main():
    event_id = str(uuid.uuid4())
    source = "step3-event-verification"
    event = {
        "specversion": "1.0",
        "id": event_id,
        "source": source,
        "type": "story.requested",
        "datacontenttype": "application/json",
        "data": {"message": "Write the season 1 story."},
    }
    publish_event(event)

    encoded_source = urllib.parse.urlencode({"source": source})
    status_url = f"{APP_URL}/events/{urllib.parse.quote(event_id)}?{encoded_source}"
    status = None
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        status = request_json(status_url)
        if status is None:
            time.sleep(2)
            continue
        if status["status"] == "awaiting_approval":
            break
        if status["status"] in ("failed", "expired"):
            raise RuntimeError(f"Event workflow ended before human review: {status}")
        time.sleep(2)
    else:
        raise TimeoutError("Event workflow did not reach human review within 180 seconds.")

    thread_id = status["thread_id"]
    print(f"[PASS] Dapr Pub/Sub event started workflow {thread_id} and reached human review")

    publish_event(event)
    duplicate_status = request_json(status_url)
    if duplicate_status is None or duplicate_status["thread_id"] != thread_id:
        raise AssertionError("Redelivered event did not resolve to its original workflow.")
    print("[PASS] Redelivering the CloudEvent resolves to the same workflow")

    request_json(f"{APP_URL}/chat/{thread_id}/decision", {
        "decision": "approve",
        "expected_revision": status.get("revision", 0),
    })
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        completed = request_json(f"{APP_URL}/chat/{thread_id}")
        if completed["status"] == "published":
            print("[PASS] Human approval resumed the event-triggered workflow through publication")
            return
        if completed["status"] in ("failed", "expired"):
            raise RuntimeError(f"Event workflow did not publish after approval: {completed}")
        time.sleep(2)
    raise TimeoutError("Approved event workflow did not publish within 300 seconds.")


if __name__ == "__main__":
    main()

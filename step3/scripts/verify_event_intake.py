"""Publish a CloudEvent through Dapr Pub/Sub and verify its workflow reaches review."""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor


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
    webhook = {
        "id": event_id,
        "source": source,
        "message": event["data"]["message"],
    }
    accepted = request_json(f"{APP_URL}/webhooks/story-requested", webhook)
    if accepted.get("status") != "ACCEPTED":
        raise AssertionError(f"Webhook gateway did not accept the event: {accepted}")

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

    with ThreadPoolExecutor(max_workers=4) as pool:
        repeated = list(pool.map(
            lambda _: request_json(f"{APP_URL}/webhooks/story-requested", webhook),
            range(4),
        ))
    if any(response.get("status") != "ACCEPTED" for response in repeated):
        raise AssertionError(f"Webhook did not accept repeated event IDs: {repeated}")
    duplicate_status = request_json(status_url)
    if duplicate_status is None or duplicate_status["thread_id"] != thread_id:
        raise AssertionError("Redelivered event did not resolve to its original workflow.")
    print("[PASS] Concurrent redeliveries of the CloudEvent resolve to the same workflow")

    review_event_id = str(uuid.uuid4())
    review_source = "step3-quality-review-verification"
    review_data = {
        "id": review_event_id,
        "source": review_source,
        "title": "The Lantern on Birch Street",
        "story": (
            "Mina found a lantern outside the library. She carried it home, "
            "then returned it when her neighbor came looking. They walked back "
            "together and discovered the library's lost-and-found box."
        ),
        "request": "Write a short, warm story about helping a neighbor.",
    }
    accepted = request_json(f"{APP_URL}/webhooks/story-quality-review-requested", review_data)
    if accepted.get("status") != "ACCEPTED":
        raise AssertionError(f"Quality-review webhook did not accept the event: {accepted}")

    encoded_source = urllib.parse.urlencode({"source": review_source})
    review_status_url = (
        f"{APP_URL}/events/quality-review/{urllib.parse.quote(review_event_id)}?{encoded_source}"
    )
    review_status = None
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        review_status = request_json(review_status_url)
        if review_status is not None and review_status["status"] in ("reviewed", "failed"):
            break
        time.sleep(2)
    if review_status is None or review_status["status"] != "reviewed":
        raise RuntimeError(f"Quality-review workflow did not complete successfully: {review_status}")
    waiting_story = request_json(status_url)
    if waiting_story is None or waiting_story["status"] != "awaiting_approval":
        raise AssertionError(
            "Quality-review execution changed the independent story workflow: "
            f"{waiting_story}"
        )

    result = review_status.get("review")
    if not isinstance(result, dict):
        raise AssertionError(f"Quality-review workflow returned no structured review: {review_status}")
    required_fields = {"score", "recommendation", "summary", "strengths", "issues", "suggestions"}
    if not required_fields.issubset(result):
        raise AssertionError(f"Quality-review result is missing fields: {result}")
    if result["recommendation"] not in ("ready", "revise") or not 1 <= result["score"] <= 5:
        raise AssertionError(f"Quality-review result has invalid decision fields: {result}")
    print(f"[PASS] Separate quality-review event completed durable workflow {review_status['thread_id']}")
    print("[PASS] Quality review returns structured advisory output without changing human approval")

    for _ in range(3):
        request_json(f"{APP_URL}/webhooks/story-quality-review-requested", review_data)
    duplicate_review_status = request_json(review_status_url)
    if duplicate_review_status is None or duplicate_review_status["thread_id"] != review_status["thread_id"]:
        raise AssertionError("Redelivered quality-review event did not resolve to its original workflow.")
    print("[PASS] Repeated quality-review CloudEvents resolve to the original workflow")
    print("[PASS] Quality-review workflow completed while story workflow remained awaiting human approval")

    request_json(f"{APP_URL}/chat/{thread_id}/decision", {
        "decision": "approve",
        "expected_revision": waiting_story.get("revision", 0),
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

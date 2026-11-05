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

    specialist_events = [
        {
            "name": "continuity-check",
            "webhook": "story-continuity-check-requested",
            "status_path": "continuity-check",
            "validator": lambda result: (
                isinstance(result.get("consistent"), bool)
                and isinstance(result.get("summary"), str)
                and isinstance(result.get("findings"), list)
            ),
        },
        {
            "name": "brief-compliance",
            "webhook": "story-brief-compliance-requested",
            "status_path": "brief-compliance",
            "validator": lambda result: (
                isinstance(result.get("compliant"), bool)
                and isinstance(result.get("summary"), str)
                and isinstance(result.get("requirements"), list)
            ),
        },
    ]
    for specialist in specialist_events:
        specialist.update({
            "id": str(uuid.uuid4()),
            "source": f"step3-{specialist['name']}-verification",
            "title": "The Lantern on Birch Street",
            "story": review_data["story"],
            "request": review_data["request"],
        })

    def submit_specialist(specialist):
        response = request_json(
            f"{APP_URL}/webhooks/{specialist['webhook']}",
            {key: specialist[key] for key in ("id", "source", "title", "story", "request")},
        )
        if response.get("status") != "ACCEPTED":
            raise AssertionError(f"{specialist['name']} webhook did not accept event: {response}")
        return specialist

    with ThreadPoolExecutor(max_workers=len(specialist_events)) as pool:
        submitted = list(pool.map(submit_specialist, specialist_events))

    for specialist in submitted:
        encoded_source = urllib.parse.urlencode({"source": specialist["source"]})
        specialist["status_url"] = (
            f"{APP_URL}/events/{specialist['status_path']}/"
            f"{urllib.parse.quote(specialist['id'])}?{encoded_source}"
        )
    deadline = time.monotonic() + 180
    pending_specialists = {item["name"]: item for item in submitted}
    while pending_specialists and time.monotonic() < deadline:
        for item in list(pending_specialists.values()):
            state = request_json(item["status_url"])
            if state is None or state["status"] not in ("reviewed", "failed"):
                continue
            if state["status"] != "reviewed":
                raise RuntimeError(f"{item['name']} workflow failed: {state}")
            result = state.get("review")
            if not isinstance(result, dict) or not item["validator"](result):
                raise AssertionError(f"{item['name']} returned invalid advisory output: {state}")
            item["thread_id"] = state["thread_id"]
            del pending_specialists[item["name"]]
            print(f"[PASS] {item['name']} event completed durable workflow {state['thread_id']}")
        if pending_specialists:
            time.sleep(2)
    if pending_specialists:
        raise TimeoutError(
            "Specialist workflows did not complete: " + ", ".join(pending_specialists)
        )

    waiting_story = request_json(status_url)
    if waiting_story is None or waiting_story["status"] != "awaiting_approval":
        raise AssertionError(
            "Specialist reviews changed the independent story workflow: "
            f"{waiting_story}"
        )
    for item in submitted:
        payload = {key: item[key] for key in ("id", "source", "title", "story", "request")}
        request_json(f"{APP_URL}/webhooks/{item['webhook']}", payload)
        duplicate = request_json(item["status_url"])
        if duplicate is None or duplicate["thread_id"] != item["thread_id"]:
            raise AssertionError(
                f"Repeated {item['name']} event did not resolve to its original workflow."
            )
    print("[PASS] Continuity and brief-compliance workflows ran independently while the story awaited approval")
    print("[PASS] Replayed specialist events resolve to their original workflow IDs")

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

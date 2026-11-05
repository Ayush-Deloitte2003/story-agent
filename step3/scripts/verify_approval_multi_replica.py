"""Verify conflicting approval decisions coordinate through Dapr across app replicas."""

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


APP_URLS = [
    value.rstrip("/")
    for value in os.getenv("APP_URLS", "http://127.0.0.1:8001,http://127.0.0.1:8002").split(",")
    if value.strip()
]


def request_json(url, payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            content = response.read()
            return response.status, json.loads(content) if content else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(detail)
        except json.JSONDecodeError:
            body = {"detail": detail}
        return exc.code, body


def main():
    if len(APP_URLS) != 2:
        raise ValueError("APP_URLS must contain the direct URLs for exactly two app replicas.")

    status, workflow = request_json(
        f"{APP_URLS[0]}/chat",
        {
            "message": (
                "Write a short story about a lighthouse keeper helping a lost sailor, "
                "then propose it for human review by calling publish_story with its title and story."
            ),
            "approval_timeout_seconds": 60,
        },
    )
    if status != 200 or workflow.get("status") != "awaiting_approval":
        raise AssertionError(f"Could not start multi-replica decision test: HTTP {status} {workflow}")

    thread_id = workflow["thread_id"]
    revision = workflow.get("revision", 0)
    decisions = [
        (
            APP_URLS[0],
            "approve",
            {"decision": "approve", "expected_revision": revision},
        ),
        (
            APP_URLS[1],
            "reject",
            {
                "decision": "reject",
                "feedback": "Make the ending more hopeful.",
                "expected_revision": revision,
            },
        ),
    ]

    def submit(item):
        url, decision, payload = item
        return decision, request_json(f"{url}/chat/{thread_id}/decision", payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, decisions))

    accepted = [(decision, result) for decision, result in results if result[0] == 200]
    refused = [(decision, result) for decision, result in results if result[0] == 409]
    if len(accepted) != 1 or len(refused) != 1:
        raise AssertionError(f"Expected one cross-replica winner and one conflict, got {results}")

    winner, (accepted_status, accepted_view) = accepted[0]
    if accepted_status != 200:
        raise AssertionError(f"Unexpected accepted response: {accepted_status} {accepted_view}")
    expected_statuses = {
        "approve": ("publishing", "published"),
        "reject": ("awaiting_approval",),
    }
    if accepted_view.get("status") not in expected_statuses[winner]:
        raise AssertionError(f"Winning decision returned an unexpected workflow state: {accepted_view}")
    if winner == "reject" and accepted_view.get("revision", 0) <= revision:
        raise AssertionError(f"Rejected draft did not advance to a new revision: {accepted_view}")
    print(f"[PASS] Conflicting decisions sent to separate app replicas: winner={winner}")
    print(f"[PASS] Shared Dapr state claim caused the losing decision to return HTTP 409")
    print(f"    workflow after winning decision: {accepted_view.get('status')}")


if __name__ == "__main__":
    main()

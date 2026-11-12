"""Measure a bounded burst of independent quality-review workflows."""

import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_URL = os.getenv("APP_URL", "http://127.0.0.1:8000").rstrip("/")
WORKFLOW_COUNT = 10
APP_REPLICAS = os.getenv("APP_REPLICAS", "1")
POLL_INTERVAL_SECONDS = 2
MAX_DURATION_SECONDS = 600
REPORT = ROOT / "verify_output_step3_kind_load.txt"
SOURCE = "step3-quality-review-load"
STORY = (
    "Nia found a lost key beside the community garden. She asked nearby neighbors "
    "for help, found its owner, and joined them for tea after the garden gate opened."
)


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
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{request.method} returned HTTP {exc.code}: {detail}") from exc
    return json.loads(content) if content else None


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction + 0.999999)))
    return ordered[index]


def workflow_status_url(event_id):
    query = urllib.parse.urlencode({"source": SOURCE})
    return f"{APP_URL}/events/quality-review/{urllib.parse.quote(event_id)}?{query}"


def submit_event(event):
    started = time.monotonic()
    try:
        response = request_json(f"{APP_URL}/webhooks/story-quality-review-requested", event)
        if response.get("status") != "ACCEPTED":
            raise RuntimeError(f"Webhook did not accept event: {response}")
        return {
            "id": event["id"],
            "started": started,
            "accepted": True,
            "error": None,
        }
    except Exception as exc:
        return {
            "id": event["id"],
            "started": started,
            "accepted": False,
            "error": str(exc),
        }


def fetch_status(run):
    try:
        return run["id"], request_json(workflow_status_url(run["id"])), None
    except Exception as exc:
        return run["id"], None, str(exc)


def main():
    lines = [
        "Quality-review workflow concurrency baseline",
        f"Started: {datetime.now(timezone.utc).isoformat()}",
        f"Target independent workflows: {WORKFLOW_COUNT}",
        f"Story-agent deployment replicas: {APP_REPLICAS}",
        f"App URL: {APP_URL}",
        "Submission mode: simultaneous webhook burst; real model-backed reviews",
        "",
    ]
    overall_started = time.monotonic()
    runs = []
    try:
        events = [{
            "id": str(uuid.uuid4()),
            "source": SOURCE,
            "title": f"Concurrency baseline story {index + 1}",
            "story": STORY,
            "request": "A concise, warm story about helping a neighbor.",
        } for index in range(WORKFLOW_COUNT)]

        with ThreadPoolExecutor(max_workers=WORKFLOW_COUNT) as pool:
            submissions = list(pool.map(submit_event, events))
        runs = submissions
        accepted = [run for run in runs if run["accepted"]]
        lines.append(f"Webhook submissions accepted: {len(accepted)}/{WORKFLOW_COUNT}")
        for run in runs:
            if not run["accepted"]:
                lines.append(f"[FAIL] Submission {run['id']}: {run['error']}")
        if not accepted:
            raise RuntimeError("No quality-review events were accepted.")

        pending = {run["id"]: run for run in accepted}
        terminal = {}
        max_active_observed = 0
        deadline = time.monotonic() + MAX_DURATION_SECONDS
        with ThreadPoolExecutor(max_workers=WORKFLOW_COUNT) as pool:
            while pending and time.monotonic() < deadline:
                polled = list(pool.map(fetch_status, pending.values()))
                active_this_sample = 0
                for event_id, state, error in polled:
                    if error:
                        pending[event_id]["last_poll_error"] = error
                        active_this_sample += 1
                        continue
                    if state is None:
                        active_this_sample += 1
                        continue
                    status = state.get("status", "unknown")
                    if status in ("reviewed", "failed", "expired"):
                        review = state.get("review")
                        valid_review = (
                            isinstance(review, dict)
                            and isinstance(review.get("score"), int)
                            and not isinstance(review.get("score"), bool)
                            and 1 <= review["score"] <= 5
                            and review.get("recommendation") in ("ready", "revise")
                            and isinstance(review.get("summary"), str)
                            and all(isinstance(review.get(field), list)
                                    and all(isinstance(value, str) for value in review[field])
                                    for field in ("strengths", "issues", "suggestions"))
                        )
                        terminal[event_id] = {
                            "status": status,
                            "elapsed_seconds": time.monotonic() - pending[event_id]["started"],
                            "has_review": valid_review,
                            "error": state.get("error"),
                        }
                        del pending[event_id]
                    else:
                        active_this_sample += 1
                max_active_observed = max(max_active_observed, active_this_sample)
                if pending:
                    time.sleep(POLL_INTERVAL_SECONDS)

        elapsed = time.monotonic() - overall_started
        successful = [item for item in terminal.values() if item["status"] == "reviewed" and item["has_review"]]
        failed = [item for item in terminal.values() if item["status"] != "reviewed" or not item["has_review"]]
        latencies = [item["elapsed_seconds"] for item in successful]
        lines.extend([
            "",
            f"Total elapsed seconds: {elapsed:.2f}",
            f"Completed reviews: {len(successful)}/{WORKFLOW_COUNT}",
            f"Terminal failures or invalid results: {len(failed)}",
            f"Unresolved after {MAX_DURATION_SECONDS}s: {len(pending)}",
            f"Maximum nonterminal workflows observed in one poll sample: {max_active_observed}",
        ])
        if latencies:
            lines.extend([
                f"Successful review latency mean seconds: {statistics.mean(latencies):.2f}",
                f"Successful review latency p50 seconds: {percentile(latencies, 0.50):.2f}",
                f"Successful review latency p95 seconds: {percentile(latencies, 0.95):.2f}",
                f"Completed-review throughput per minute: {len(successful) * 60 / elapsed:.2f}",
            ])
        for event_id, item in terminal.items():
            lines.append(f"[{item['status'].upper()}] {event_id}: {item['elapsed_seconds']:.2f}s")
            if item["error"]:
                error = " ".join(str(item["error"]).split())
                lines.append(f"  Error: {error[:500]}")
        for event_id, run in pending.items():
            lines.append(f"[UNRESOLVED] {event_id}: {run.get('last_poll_error', 'workflow did not finish')}")

        if len(successful) != WORKFLOW_COUNT or failed or pending:
            lines.append("LOAD CHECK FAILED")
            return_code = 1
        else:
            lines.append(f"ALL {WORKFLOW_COUNT} QUALITY-REVIEW WORKFLOWS COMPLETED")
            return_code = 0
    except Exception as exc:
        lines.extend(["", f"LOAD CHECK FAILED: {type(exc).__name__}: {exc}"])
        return_code = 1
    finally:
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines), flush=True)
        print(f"Load report saved to {REPORT}", flush=True)

    raise SystemExit(return_code)


if __name__ == "__main__":
    main()

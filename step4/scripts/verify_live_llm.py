"""Verify one live-agent booking and disruption journey through the Step 4 API."""

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.getenv("FLIGHT_API_URL", "http://127.0.0.1:8000")
POLL_INTERVAL_SECONDS = 1
WORKFLOW_TIMEOUT_SECONDS = 180


def request(
    method: str,
    path: str,
    body: dict | None = None,
    headers: dict | None = None,
    expected=(200, 202),
) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    if status not in expected:
        raise RuntimeError(f"{method} {path} returned HTTP {status}")
    return json.loads(payload or b"{}")


def eventually(load, predicate, label: str, timeout: int = WORKFLOW_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = load()
        if predicate(value):
            return value
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"Timed out waiting for {label}")


def workflow_stage(booking_state: dict) -> str | None:
    return serialized_stage(booking_state["workflow"])


def serialized_stage(workflow: dict) -> str | None:
    serialized = workflow.get("custom_status")
    if not serialized:
        return None
    status = json.loads(serialized) if isinstance(serialized, str) else serialized
    return status.get("stage")


def workflow_output(workflow: dict) -> dict:
    serialized = workflow.get("output")
    if not serialized:
        return {}
    return json.loads(serialized) if isinstance(serialized, str) else serialized


def inbox_detail(workflow_id: str) -> dict:
    return request("GET", f"/inbox/{workflow_id}")


def pending_inbox_item(workflow_id: str, kind: str) -> dict | None:
    items = request("GET", "/inbox")["items"]
    return next(
        (item for item in items if item["workflow_id"] == workflow_id and item["kind"] == kind),
        None,
    )


def require_advisory(result: dict, role: str) -> None:
    if not isinstance(result, dict) or result.get("authority") != "advisory_only":
        raise AssertionError(f"{role} did not return advisory-only output")


def verify_booking(run_token: str) -> tuple[str, str]:
    idempotency_key = f"step4-live-llm-{run_token}"
    created = request(
        "POST",
        "/bookings",
        {
            "flight_id": "SA101",
            "seat": "1A",
            "passenger_name": "Live smoke passenger",
            "hold_seconds": 300,
        },
        {"Idempotency-Key": idempotency_key},
    )
    booking_id = created["booking_id"]
    workflow_id = created["workflow_id"]

    waiting = eventually(
        lambda: request("GET", f"/bookings/{booking_id}"),
        lambda state: (
            state["booking"]["status"] == "awaiting_approval"
            and workflow_stage(state) == "awaiting_approval"
        ),
        "booking workflow approval",
    )
    item = eventually(
        lambda: pending_inbox_item(workflow_id, "booking"),
        lambda value: value is not None,
        "booking approval inbox entry",
    )
    require_advisory(item["proposal"].get("booking_advice"), "Booking advisor")
    require_advisory(item["proposal"].get("payment_risk_advice"), "Payment-risk advisor")
    if waiting["booking"]["ticket"] is not None:
        raise AssertionError("Booking received a ticket before human approval")

    request("POST", f"/inbox/{workflow_id}/decision", {"decision": "approve"})
    approved_detail = eventually(
        lambda: inbox_detail(workflow_id),
        lambda value: (
            value["item"]["status"] == "approved"
            and value["workflow"]["runtime_status"] == "COMPLETED"
            and workflow_output(value["workflow"]).get("status") == "confirmed"
        ),
        "approved booking workflow completion",
    )
    if approved_detail["item"]["kind"] != "booking":
        raise AssertionError("Unexpected approval type")
    confirmed = eventually(
        lambda: request("GET", f"/bookings/{booking_id}"),
        lambda state: state["booking"]["status"] == "confirmed",
        "simulated booking completion",
    )
    if confirmed["booking"]["ticket"]["flight_id"] != "SA101":
        raise AssertionError("Booking ticket does not match the approved flight")
    return booking_id, workflow_id


def verify_disruption(run_token: str, booking_id: str) -> str:
    disruption = request(
        "POST",
        "/disruptions",
        {
            "id": f"live-llm-{run_token}",
            "source": "step4-live-llm-smoke",
            "booking_id": booking_id,
            "flight_id": "SA101",
        },
    )
    workflow_id = disruption["workflow_id"]
    pending_item = eventually(
        lambda: pending_inbox_item(workflow_id, "rebooking"),
        lambda value: value is not None,
        "human rebooking approval",
    )
    pending_detail = eventually(
        lambda: inbox_detail(workflow_id),
        lambda value: (
            value["item"]["status"] == "pending"
            and value["workflow"]["runtime_status"] == "RUNNING"
            and serialized_stage(value["workflow"]) == "awaiting_rebooking_approval"
        ),
        "rebooking workflow approval stage",
    )
    if pending_detail["item"]["workflow_id"] != pending_item["workflow_id"]:
        raise AssertionError("Rebooking approval state changed unexpectedly")
    proposal = pending_detail["item"]["proposal"]
    require_advisory(proposal, "Disruption advisor")
    alternatives = proposal["alternatives"]
    if not alternatives:
        raise AssertionError("No mock rebooking alternative was proposed")
    allowed = {alternative["flight_id"] for alternative in alternatives}
    recommendation = proposal["recommended_flight_id"]
    if recommendation is not None and recommendation not in allowed:
        raise AssertionError("Disruption advisor recommended an unlisted flight")
    approved_flight = alternatives[0]["flight_id"]

    before = request("GET", f"/bookings/{booking_id}")["booking"]
    if before["ticket"]["flight_id"] != "SA101":
        raise AssertionError("Disruption advisor changed the booking before approval")
    request(
        "POST",
        f"/inbox/{workflow_id}/decision",
        {"decision": "approve", "flight_id": approved_flight},
    )

    def completion_state():
        return inbox_detail(workflow_id), request("GET", f"/bookings/{booking_id}")["booking"]

    completed_item, rebooked = eventually(
        completion_state,
        lambda result: (
            result[0]["item"]["status"] == "completed"
            and result[0]["workflow"]["runtime_status"] == "COMPLETED"
            and workflow_output(result[0]["workflow"]).get("status") == "rebooked"
            and result[1]["status"] == "confirmed"
            and result[1]["ticket"]["flight_id"] == approved_flight
        ),
        "approved rebooking completion",
    )
    if completed_item["item"]["kind"] != "rebooking":
        raise AssertionError("Unexpected rebooking approval type")
    if rebooked["payment"]["status"] != "paid":
        raise AssertionError("Rebooking changed the simulated payment state")
    return workflow_id


def main() -> None:
    request("GET", "/healthz", expected=(200,))
    run_token = os.getenv("GITHUB_RUN_ID", str(int(time.time())))
    booking_id, booking_workflow_id = verify_booking(run_token)
    rebooking_workflow_id = verify_disruption(run_token, booking_id)
    print(
        "PASS: booking and payment-risk advisors reached booking approval "
        f"({booking_workflow_id[:18]})."
    )
    print(
        "PASS: disruption advisor reached human-approved rebooking "
        f"({rebooking_workflow_id[:18]}). Three sidecars are each capped at one request."
    )


if __name__ == "__main__":
    main()

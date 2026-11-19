import concurrent.futures
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

BASE = os.getenv("FLIGHT_API_URL", "http://127.0.0.1:8000")


def request(method, path, body=None, headers=None, expected=(200, 202)):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.loads(response.read() or b"{}")
            if response.status not in expected:
                raise AssertionError(f"{method} {path}: unexpected HTTP {response.status}")
            return response.status, result
    except urllib.error.HTTPError as exc:
        result = json.loads(exc.read() or b"{}")
        if exc.code not in expected:
            raise AssertionError(f"{method} {path}: HTTP {exc.code}: {result}") from exc
        return exc.code, result


def eventually(load, predicate, label, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = load()
        if predicate(value):
            return value
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting for {label}; last value: {value}")


def submit(key, flight, seat, failure_at=None, hold_seconds=300):
    body = {
        "flight_id": flight, "seat": seat, "passenger_name": "Test Passenger",
        "hold_seconds": hold_seconds, "failure_at": failure_at,
    }
    return request("POST", "/bookings", body, {"Idempotency-Key": key})[1]


def booking(booking_id):
    return request("GET", f"/bookings/{booking_id}")[1]


def await_status(booking_id, status, timeout=60):
    return eventually(
        lambda: booking(booking_id),
        lambda result: result["booking"]["status"] == status,
        f"booking {booking_id} status {status}",
        timeout,
    )


def approve(workflow_id, flight_id=None):
    body = {"decision": "approve"}
    if flight_id:
        body["flight_id"] = flight_id
    request("POST", f"/inbox/{workflow_id}/decision", body)


def await_inbox(workflow_id, timeout=30):
    return eventually(
        lambda: request("GET", "/inbox")[1]["items"],
        lambda items: any(item["workflow_id"] == workflow_id for item in items),
        f"inbox item {workflow_id}",
        timeout,
    )


def verify_restart_recovery():
    pending = submit("restart-recovery", "SA102", "2A")
    await_inbox(pending["workflow_id"])
    pod = subprocess.check_output(
        ["kubectl", "get", "pods", "-l", "app=booking-worker", "-o", "jsonpath={.items[0].metadata.name}"],
        text=True,
    ).strip()
    if not pod:
        raise AssertionError("No booking-worker pod found for restart check")
    subprocess.run(["kubectl", "delete", "pod", pod, "--wait=false"], check=True, capture_output=True, text=True)
    subprocess.run(["kubectl", "rollout", "status", "deployment/booking-worker", "--timeout=180s"],
                   check=True, capture_output=True, text=True)
    approve(pending["workflow_id"])
    result = await_status(pending["booking_id"], "confirmed")
    assert result["booking"]["ticket"], "Ticket was not persisted after worker restart"
    print("[PASS] Workflow resumed after booking-worker pod replacement")


def verify_ambient_feed(confirmed):
    """A seeded feed row must start a rebooking via the poller, still gated by human approval."""
    booking_id = confirmed["booking_id"]
    before = booking(booking_id)["booking"]
    flight = before["ticket"]["flight_id"]
    feed_id = f"verify-feed-{int(time.time())}"
    row = {"source_event_id": feed_id, "flight_id": flight, "status": "cancelled"}
    assert request("POST", "/simulation/ops-feed", row)[1]["status"] == "seeded"
    assert request("POST", "/simulation/ops-feed", row)[1]["status"] == "duplicate"
    bad_id = feed_id + "-bad"
    request("POST", "/simulation/ops-feed", {"source_event_id": bad_id, "flight_id": flight, "status": "exploded"})

    def pending():
        return [item for item in request("GET", "/inbox")[1]["items"]
                if item["booking_id"] == booking_id and item["kind"] == "rebooking"]

    items = eventually(pending, bool, f"poller-started rebooking for {booking_id}", timeout=60)
    status = eventually(lambda: request("GET", "/ops-feed/status")[1],
                        lambda value: value["counters"]["quarantined"] >= 1, "bad feed row quarantined")
    assert status["counters"]["emitted"] >= 1, status
    time.sleep(3)
    assert len(pending()) == 1, "Repeated polls created extra rebooking approvals"
    unchanged = booking(booking_id)["booking"]
    assert unchanged["status"] == "confirmed" and unchanged["ticket"]["flight_id"] == flight, \
        "Booking changed without human approval"
    assert items[0]["status"] == "pending"
    print("[PASS] Poller turned a feed row into a rebooking workflow; approval still required")


def main():
    request("GET", "/healthz")
    print("[PASS] API and PostgreSQL are ready")

    happy = submit("booking-idempotency", "SA101", "1A")
    duplicate = submit("booking-idempotency", "SA101", "1A")
    assert happy["booking_id"] == duplicate["booking_id"], "Retry created another booking"
    status, _ = request(
        "POST", "/bookings",
        {"flight_id": "SA101", "seat": "1B", "passenger_name": "Different"},
        {"Idempotency-Key": "booking-idempotency"}, expected=(409,),
    )
    assert status == 409, "Idempotency key reuse with changed input was accepted"
    await_inbox(happy["workflow_id"])
    approve(happy["workflow_id"])
    await_status(happy["booking_id"], "confirmed")
    print("[PASS] Booking idempotency and human approval")

    ticket_failure = submit("ticketing-compensation", "SA101", "1B", "ticketing")
    await_inbox(ticket_failure["workflow_id"])
    approve(ticket_failure["workflow_id"])
    result = await_status(ticket_failure["booking_id"], "compensated")
    assert result["booking"]["payment"]["status"] == "refunded"
    assert result["booking"]["seat_hold"] is None
    print("[PASS] Ticketing failure triggers idempotent refund and seat release")

    payment_failure = submit("payment-failure", "SA101", "2A", "payment")
    await_inbox(payment_failure["workflow_id"])
    approve(payment_failure["workflow_id"])
    result = await_status(payment_failure["booking_id"], "compensated")
    assert result["booking"]["payment"] is None and result["booking"]["seat_hold"] is None
    print("[PASS] Payment failure releases the seat without creating a payment")

    expiring = submit("hold-expiry", "SA102", "1A", hold_seconds=4)
    result = await_status(expiring["booking_id"], "compensated", timeout=30)
    workflow = result["workflow"]
    assert json.loads(workflow["output"])["status"] == "expired"
    assert result["booking"]["seat_hold"] is None
    print("[PASS] Expiring hold is compensated")

    requests = [
        ("contention-a", "SA102", "1B"),
        ("contention-b", "SA102", "1B"),
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        contenders = list(pool.map(lambda item: submit(*item), requests))
    for candidate in contenders:
        eventually(
            lambda candidate=candidate: booking(candidate["booking_id"]),
            lambda value: value["booking"]["status"] in ("awaiting_approval", "seat_unavailable"),
            f"contention result for {candidate['booking_id']}",
        )
    states = [booking(candidate["booking_id"])["booking"]["status"] for candidate in contenders]
    assert states.count("awaiting_approval") == 1 and states.count("seat_unavailable") == 1, states
    winner = next(candidate for candidate in contenders if booking(candidate["booking_id"])["booking"]["status"] == "awaiting_approval")
    approve(winner["workflow_id"])
    await_status(winner["booking_id"], "confirmed")
    print("[PASS] Concurrent seat requests yield one hold")

    verify_restart_recovery()

    disrupted = happy["booking_id"]
    event = {"id": "disruption-event-1", "source": "step4-verification",
             "booking_id": disrupted, "flight_id": "SA101"}
    first = request("POST", "/disruptions", event)[1]
    second = request("POST", "/disruptions", event)[1]
    assert first["workflow_id"] == second["workflow_id"]
    assert (first["status"], second["status"]) == ("accepted", "duplicate"), (first, second)
    status, _ = request("POST", "/disruptions", {**event, "flight_id": "SA201"}, expected=(409,))
    assert status == 409, "Event id reuse with a different payload was accepted"
    for bad in ({"id": "bad-1", "source": "step4-verification", "booking_id": disrupted},
                {"id": "bad-2", "source": "step4-verification", "type": "com.example.unknown",
                 "data": {"booking_id": disrupted, "flight_id": "SA101"}}):
        quarantined = request("POST", "/disruptions", bad)[1]
        assert quarantined["status"] == "quarantined", quarantined
    items = await_inbox(first["workflow_id"])
    recommendation = next(item for item in items if item["workflow_id"] == first["workflow_id"])
    time.sleep(2)
    duplicates = [
        item for item in request("GET", "/inbox")[1]["items"]
        if item["workflow_id"] == first["workflow_id"]
    ]
    assert len(duplicates) == 1, f"Duplicate event created extra inbox rows: {duplicates}"
    alternative = recommendation["proposal"]["alternatives"][0]["flight_id"]
    approve(first["workflow_id"], alternative)
    result = eventually(
        lambda: booking(disrupted),
        lambda value: value["booking"]["ticket"]["flight_id"] == alternative,
        "approved rebooking",
    )
    assert result["booking"]["payment"]["status"] == "paid"
    print("[PASS] Duplicate disruption is deduplicated; human-approved rebooking preserves original payment")

    verify_ambient_feed(winner)
    print("ALL STEP 4 CHECKS PASSED")


if __name__ == "__main__":
    main()

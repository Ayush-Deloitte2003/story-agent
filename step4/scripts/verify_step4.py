import concurrent.futures
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

BASE = os.getenv("FLIGHT_API_URL", "http://127.0.0.1:8000")
SUPPORT_BASE = os.getenv("SUPPORT_SERVICE_URL", "http://127.0.0.1:8001")


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


def support_request(body, expected=(200,)):
    req = urllib.request.Request(
        SUPPORT_BASE + "/support/ask", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code not in expected:
            raise AssertionError(f"support ask: HTTP {exc.code}") from exc
        return exc.code, json.loads(exc.read() or b"{}")


def verify_support(booking_id):
    before = booking(booking_id)["booking"]
    _, answer = support_request(
        {"booking_id": booking_id, "question": "Can I cancel and get a refund? Mail me at a.b@example.com"})
    assert answer["authority"] == "advisory_only" and answer["source"] == "advisor", answer
    assert answer["suggested_action"] == "request_cancellation" and answer["requires_human_approval"], answer
    assert "example.com" not in json.dumps(answer), answer
    after = booking(booking_id)["booking"]
    assert before["status"] == after["status"] == "confirmed" and after["ticket"] == before["ticket"]
    status, _ = support_request({"booking_id": "booking-missing", "question": "status?"}, expected=(404,))
    assert status == 404
    with urllib.request.urlopen(SUPPORT_BASE + "/support/status", timeout=10) as response:
        counters = json.loads(response.read())["counters"]
    assert counters["advisor"] >= 1 and counters["not_found"] >= 1, counters
    print("[PASS] Support advisor answers read-only; booking unchanged; unknown booking is 404")


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


def confirmed_booking(key, flight, seat):
    pending = submit(key, flight, seat)
    await_inbox(pending["workflow_id"])
    approve(pending["workflow_id"])
    await_status(pending["booking_id"], "confirmed")
    return pending


def cancel(key, booking_id, approval_seconds=300, expected=(202,)):
    return request("POST", "/cancellations", {"booking_id": booking_id, "approval_seconds": approval_seconds},
                   {"Idempotency-Key": key}, expected=expected)


def inbox_item(workflow_id, status=None):
    def load():
        if status in (None, "pending"):
            return next((item for item in request("GET", "/inbox")[1]["items"]
                         if item["workflow_id"] == workflow_id), None)
        return request("GET", f"/inbox/{workflow_id}")[1]["item"]
    return eventually(load, lambda item: item is not None and (status is None or item["status"] == status),
                      f"inbox {workflow_id} status {status}", 60)


def decide_cancellation(item, decision="approve", **overrides):
    body = {"decision": decision}
    if decision == "approve":
        body.update(request_hash=item["proposal"]["request_hash"], refund_cents=item["proposal"]["refund_cents"])
    body.update(overrides)
    return request("POST", f"/inbox/{item['workflow_id']}/decision", body, expected=(200, 409, 422))


def verify_cancellations():
    stamp = int(time.time())
    first = confirmed_booking(f"cancel-seed-a-{stamp}", "SA201", "1A")
    second = confirmed_booking(f"cancel-seed-b-{stamp}", "SA201", "1B")

    # Approval path: a simulated refund at the deterministic 100% tier, then the seat is released.
    key = f"cancel-a-{stamp}"
    accepted = cancel(key, first["booking_id"])[1]
    assert accepted["status"] == "scheduled" and accepted["workflow_id"].startswith("cancellation-"), accepted
    duplicate = cancel(key, first["booking_id"])[1]
    assert duplicate["workflow_id"] == accepted["workflow_id"], (accepted, duplicate)
    status, _ = request("POST", "/cancellations", {"booking_id": second["booking_id"]},
                        {"Idempotency-Key": key}, expected=(409,))
    assert status == 409, "Idempotency key reuse with a different booking was accepted"
    item = inbox_item(accepted["workflow_id"], "pending")
    assert item["kind"] == "cancellation" and item["proposal"]["refund_cents"] == 14500, item
    assert booking(first["booking_id"])["booking"]["status"] == "confirmed", "Booking changed before approval"
    status, _ = decide_cancellation(item, request_hash=None)
    assert status == 422, "Approval without request_hash was accepted"
    status, _ = decide_cancellation(item, refund_cents=1)
    assert status == 409, "Approval for a different refund amount was accepted"
    time.sleep(1)
    assert booking(first["booking_id"])["booking"]["status"] == "confirmed", "Booking changed without valid approval"
    assert decide_cancellation(item)[0] == 200
    await_status(first["booking_id"], "cancelled")
    assert inbox_item(accepted["workflow_id"], "completed")["status"] == "completed"
    # A repeated request after completion is refused, and the released seat can be booked again.
    cancel(f"cancel-a-again-{stamp}", first["booking_id"], expected=(409,))
    rebooked = submit(f"cancel-seat-reuse-{stamp}", "SA201", "1A")
    await_status(rebooked["booking_id"], "awaiting_approval")
    print("[PASS] Cancellation approved: simulated refund recorded, booking cancelled, seat released")

    # Rejection and expiry leave the booking confirmed and allow a new request.
    rejected = cancel(f"cancel-b-reject-{stamp}", second["booking_id"])[1]
    decide_cancellation(inbox_item(rejected["workflow_id"], "pending"), "reject")
    inbox_item(rejected["workflow_id"], "rejected")
    assert booking(second["booking_id"])["booking"]["status"] == "confirmed"
    expiring = cancel(f"cancel-b-expire-{stamp}", second["booking_id"], approval_seconds=4)[1]
    inbox_item(expiring["workflow_id"], "expired")
    assert booking(second["booking_id"])["booking"]["status"] == "confirmed"
    final = cancel(f"cancel-b-final-{stamp}", second["booking_id"])[1]
    assert decide_cancellation(inbox_item(final["workflow_id"], "pending"))[0] == 200
    await_status(second["booking_id"], "cancelled")
    print("[PASS] Cancellation rejection and expiry keep the booking; a new request can then complete")


def main():
    request("GET", "/healthz")
    print("[PASS] API and PostgreSQL are ready")

    with urllib.request.urlopen(BASE + "/ui", timeout=15) as page:
        html = page.read().decode()
        assert page.status == 200 and "text/html" in page.headers["Content-Type"], page.status
        assert "Step 4 reviewer UI" in html and "innerHTML" not in html
    print("[PASS] Reviewer UI page is served (layout needs a human check)")

    happy = submit("booking-idempotency", "SA101", "1A")
    duplicate = submit("booking-idempotency", "SA101", "1A")
    assert happy["booking_id"] == duplicate["booking_id"], "Retry created another booking"
    status, _ = request(
        "POST", "/bookings",
        {"flight_id": "SA101", "seat": "1B", "passenger_name": "Different"},
        {"Idempotency-Key": "booking-idempotency"}, expected=(409,),
    )
    assert status == 409, "Idempotency key reuse with changed input was accepted"
    items = await_inbox(happy["workflow_id"])
    proposal = next(item for item in items if item["workflow_id"] == happy["workflow_id"])["proposal"]
    fare = proposal["fare_advice"]
    assert fare["authority"] == "advisory_only" and fare["comparison"][0]["flight_id"] == "SA101", fare
    assert {o["flight_id"] for o in fare["comparison"]} == set(fare["ranked_flight_ids"]), fare
    approve(happy["workflow_id"])
    confirmed = await_status(happy["booking_id"], "confirmed")
    assert confirmed["booking"]["ticket"]["flight_id"] == "SA101", "Fare advice changed the booked flight"
    print("[PASS] Booking idempotency and human approval")
    print("[PASS] Fare advice appears in the approval proposal and does not change the booked flight")
    verify_support(happy["booking_id"])

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

    verify_cancellations()
    verify_ambient_feed(winner)
    print("ALL STEP 4 CHECKS PASSED")


if __name__ == "__main__":
    main()

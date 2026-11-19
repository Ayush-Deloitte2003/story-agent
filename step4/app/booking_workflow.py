import json
import logging
import os
from datetime import timedelta
import urllib.request

import dapr.ext.workflow as wf

from app.agent_contracts import validate_booking_advice, validate_payment_risk_advice
from app.inventory import get_flight
from app.store import PostgresStore

log = logging.getLogger("step4.booking-workflow")
runtime = wf.WorkflowRuntime()
store = PostgresStore()
RETRY = wf.RetryPolicy(first_retry_interval=timedelta(seconds=1), max_number_of_attempts=3, backoff_coefficient=2)


@runtime.workflow(name="flight_booking_workflow")
def flight_booking_workflow(ctx: wf.DaprWorkflowContext, inp: dict):
    booking_id = inp["booking_id"]
    workflow_id = ctx.instance_id
    request = inp["request"]
    _status(ctx, "getting_booking_advice", booking_id=booking_id)
    advice = yield ctx.call_activity(
        booking_policy_advice,
        input={"flight_id": request["flight_id"], "seat": request["seat"]},
    )
    advice = validate_booking_advice(advice, request["flight_id"], request["seat"])
    _status(ctx, "holding_seat", booking_id=booking_id)
    held = yield ctx.call_activity(
        hold_seat,
        input={"booking_id": booking_id, "request": request},
        retry_policy=RETRY,
    )
    if not held:
        yield ctx.call_activity(mark_booking, input={"booking_id": booking_id, "status": "seat_unavailable"})
        return {"status": "seat_unavailable", "booking_id": booking_id}

    expires_at = ctx.current_utc_datetime + timedelta(seconds=request["hold_seconds"])
    try:
        _status(ctx, "assessing_payment_risk", booking_id=booking_id)
        payment_advice = yield ctx.call_activity(
            payment_risk_advice,
            input={"flight_id": request["flight_id"], "seat": request["seat"],
                   "amount_cents": get_flight(request["flight_id"])["price_cents"]},
        )
        payment_advice = validate_payment_risk_advice(payment_advice)
        yield ctx.call_activity(
            open_booking_approval,
            input={"workflow_id": workflow_id, "booking_id": booking_id, "request": request,
                   "expires_at": expires_at.isoformat(), "advice": advice,
                   "payment_advice": payment_advice},
            retry_policy=RETRY,
        )
    except (wf.TaskFailedError, ValueError) as exc:
        yield ctx.call_activity(
            compensate_booking,
            input={"workflow_id": workflow_id, "booking_id": booking_id, "reason": "failed"},
            retry_policy=RETRY,
        )
        return {"status": "compensated", "booking_id": booking_id, "error": str(exc)}
    _status(ctx, "awaiting_approval", booking_id=booking_id, expires_at=expires_at.isoformat())
    approval = ctx.wait_for_external_event("approval")
    timer = ctx.create_timer(expires_at)
    winner = yield wf.when_any([approval, timer])
    if winner == timer:
        yield ctx.call_activity(
            compensate_booking,
            input={"workflow_id": workflow_id, "booking_id": booking_id, "reason": "expired"},
            retry_policy=RETRY,
        )
        return {"status": "expired", "booking_id": booking_id}

    decision = approval.get_result() or {}
    if decision.get("decision") != "approve":
        yield ctx.call_activity(
            compensate_booking,
            input={"workflow_id": workflow_id, "booking_id": booking_id, "reason": "rejected"},
            retry_policy=RETRY,
        )
        return {"status": "rejected", "booking_id": booking_id}

    flight = get_flight(request["flight_id"])
    try:
        _status(ctx, "simulating_payment", booking_id=booking_id)
        yield ctx.call_activity(
            simulate_payment,
            input={"booking_id": booking_id, "amount_cents": flight["price_cents"],
                   "failure_at": request.get("failure_at")},
            retry_policy=RETRY,
        )
        _status(ctx, "issuing_ticket", booking_id=booking_id)
        ticket = yield ctx.call_activity(
            issue_ticket,
            input={"booking_id": booking_id, "flight_id": request["flight_id"],
                   "seat": request["seat"], "failure_at": request.get("failure_at")},
            retry_policy=RETRY,
        )
    except wf.TaskFailedError as exc:
        log.warning("Booking %s failed after approval; compensating: %s", booking_id, exc)
        yield ctx.call_activity(
            compensate_booking,
            input={"workflow_id": workflow_id, "booking_id": booking_id, "reason": "failed"},
            retry_policy=RETRY,
        )
        return {"status": "compensated", "booking_id": booking_id, "error": str(exc)}
    return {"status": "confirmed", "booking_id": booking_id, "ticket": ticket}


def _status(ctx, stage: str, **fields) -> None:
    ctx.set_custom_status(json.dumps({"stage": stage, **fields}))


@runtime.activity(name="hold_seat")
async def hold_seat(ctx: wf.WorkflowActivityContext, inp: dict) -> bool:
    req = inp["request"]
    return store.reserve_seat(inp["booking_id"], req["flight_id"], req["seat"], req["hold_seconds"])


@runtime.activity(name="booking_policy_advice")
async def booking_policy_advice(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    url = os.getenv("BOOKING_ADVISOR_URL", "http://127.0.0.1:8101/advise")
    request = urllib.request.Request(
        url,
        data=json.dumps(inp).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


@runtime.activity(name="payment_risk_advice")
async def payment_risk_advice(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    url = os.getenv("PAYMENT_ADVISOR_URL", "http://127.0.0.1:8102/assess")
    request = urllib.request.Request(
        url,
        data=json.dumps(inp).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


@runtime.activity(name="open_booking_approval")
async def open_booking_approval(ctx: wf.WorkflowActivityContext, inp: dict) -> None:
    req = inp["request"]
    flight = get_flight(req["flight_id"])
    store.create_booking_approval(
        inp["workflow_id"], inp["booking_id"], inp["expires_at"],
        {"flight_id": req["flight_id"], "seat": req["seat"], "passenger_name": req["passenger_name"],
         "price_cents": flight["price_cents"], "booking_advice": inp["advice"],
         "payment_risk_advice": inp["payment_advice"]},
    )


@runtime.activity(name="simulate_payment")
async def simulate_payment(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    return store.simulate_payment(inp["booking_id"], inp["amount_cents"], inp.get("failure_at"))


@runtime.activity(name="issue_ticket")
async def issue_ticket(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    return store.issue_ticket(inp["booking_id"], inp["flight_id"], inp["seat"], inp.get("failure_at"))


@runtime.activity(name="compensate_booking")
async def compensate_booking(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    return store.compensate(inp["booking_id"], inp["workflow_id"], inp["reason"])


@runtime.activity(name="mark_booking")
async def mark_booking(ctx: wf.WorkflowActivityContext, inp: dict) -> None:
    store.mark_booking(inp["booking_id"], inp["status"])

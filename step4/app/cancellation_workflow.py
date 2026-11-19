import json
import logging
import os
import urllib.request
from datetime import timedelta

import dapr.ext.workflow as wf

from app.agent_contracts import validate_cancellation_advice
from app.contracts import cancellation_request_hash
from app.inventory import refund_for
from app.store import PostgresStore

log = logging.getLogger("step4.cancellation-workflow")
runtime = wf.WorkflowRuntime()
store = PostgresStore()
RETRY = wf.RetryPolicy(first_retry_interval=timedelta(seconds=1), max_number_of_attempts=3, backoff_coefficient=2)


@runtime.workflow(name="flight_cancellation_workflow")
def flight_cancellation_workflow(ctx: wf.DaprWorkflowContext, inp: dict):
    workflow_id = ctx.instance_id
    booking_id = inp["booking_id"]
    snapshot = inp["snapshot"]
    failure_at = inp["request"].get("failure_at")
    # The authoritative amount comes only from the snapshot taken when the request was accepted.
    fare = refund_for(snapshot["paid_cents"], snapshot["depart_at"], snapshot["requested_at"])
    request_hash = cancellation_request_hash(
        workflow_id, booking_id, snapshot, fare["refund_cents"], fare["fare_rule"])

    _status(ctx, "getting_policy_advice", booking_id=booking_id)
    advice, advice_note = None, None
    try:
        advice = yield ctx.call_activity(
            cancellation_policy_advice,
            input={"flight_id": snapshot["flight_id"], "paid_cents": snapshot["paid_cents"],
                   "depart_at": snapshot["depart_at"], "requested_at": snapshot["requested_at"]},
        )
        advice = validate_cancellation_advice(advice, fare)
    except wf.TaskFailedError:
        advice, advice_note = None, "advisor_unavailable"
    except ValueError as exc:
        log.warning("Cancellation advice rejected for %s; using deterministic fare rule", booking_id)
        advice, advice_note = None, f"advice_rejected: {exc}"[:200]

    expires_at = ctx.current_utc_datetime + timedelta(seconds=inp["request"].get("approval_seconds", 300))
    proposal = {
        "cancellation_id": workflow_id, "booking_id": booking_id, "flight_id": snapshot["flight_id"],
        "seat": snapshot["seat"], "paid_cents": snapshot["paid_cents"],
        "refund_cents": fare["refund_cents"], "fare_rule": fare["fare_rule"],
        "eligibility": fare["eligibility"], "request_hash": request_hash,
        "advice": advice, "advice_note": advice_note,
    }
    opened = yield ctx.call_activity(
        open_cancellation_approval,
        input={"workflow_id": workflow_id, "booking_id": booking_id, "snapshot": snapshot,
               "expires_at": expires_at.isoformat(), "proposal": proposal},
        retry_policy=RETRY,
    )
    if not opened["opened"]:
        return {"status": opened["status"], "booking_id": booking_id}
    _status(ctx, "awaiting_cancellation_approval", booking_id=booking_id,
            refund_cents=fare["refund_cents"], expires_at=expires_at.isoformat())
    approval = ctx.wait_for_external_event("approval")
    timer = ctx.create_timer(expires_at)
    winner = yield wf.when_any([approval, timer])
    if winner == timer:
        yield ctx.call_activity(close_cancellation, input={"workflow_id": workflow_id, "status": "expired"},
                                retry_policy=RETRY)
        return {"status": "expired", "booking_id": booking_id}

    decision = approval.get_result() or {}
    if decision.get("decision") != "approve":
        yield ctx.call_activity(close_cancellation, input={"workflow_id": workflow_id, "status": "rejected"},
                                retry_policy=RETRY)
        return {"status": "rejected", "booking_id": booking_id}
    if (decision.get("request_hash") != request_hash
            or decision.get("refund_cents") != fare["refund_cents"]):
        yield ctx.call_activity(close_cancellation, input={"workflow_id": workflow_id, "status": "stale"},
                                retry_policy=RETRY)
        return {"status": "stale_approval", "booking_id": booking_id}

    _status(ctx, "issuing_simulated_refund", booking_id=booking_id)
    try:
        refund = yield ctx.call_activity(
            issue_refund,
            input={"workflow_id": workflow_id, "booking_id": booking_id, "snapshot": snapshot,
                   "refund_cents": fare["refund_cents"], "failure_at": failure_at},
            retry_policy=RETRY,
        )
    except wf.TaskFailedError as exc:
        log.warning("Refund failed for cancellation %s; booking left confirmed", workflow_id)
        yield ctx.call_activity(close_cancellation, input={"workflow_id": workflow_id, "status": "refund_failed"},
                                retry_policy=RETRY)
        return {"status": "refund_failed", "booking_id": booking_id, "error": str(exc)}
    if refund["result"] != "issued":
        status = "stale" if refund["result"] in ("stale", "not_confirmed") else "failed"
        yield ctx.call_activity(close_cancellation, input={"workflow_id": workflow_id, "status": status},
                                retry_policy=RETRY)
        return {"status": refund["result"], "booking_id": booking_id}

    _status(ctx, "releasing_seat", booking_id=booking_id)
    try:
        yield ctx.call_activity(
            release_seat,
            input={"workflow_id": workflow_id, "booking_id": booking_id, "failure_at": failure_at},
            retry_policy=RETRY,
        )
    except wf.TaskFailedError as exc:
        log.warning("Seat release failed for cancellation %s; reversing simulated refund", workflow_id)
        yield ctx.call_activity(
            reverse_refund, input={"workflow_id": workflow_id, "booking_id": booking_id}, retry_policy=RETRY)
        return {"status": "failed", "booking_id": booking_id, "error": str(exc)}
    return {"status": "cancelled", "booking_id": booking_id, "refund_cents": refund["amount_cents"]}


def _status(ctx, stage: str, **fields) -> None:
    ctx.set_custom_status(json.dumps({"stage": stage, **fields}))


@runtime.activity(name="cancellation_policy_advice")
async def cancellation_policy_advice(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    url = os.getenv("CANCELLATION_ADVISOR_URL", "http://127.0.0.1:8103/advise")
    request = urllib.request.Request(
        url, data=json.dumps(inp).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


@runtime.activity(name="open_cancellation_approval")
async def open_cancellation_approval(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    return store.open_cancellation_approval(
        inp["workflow_id"], inp["booking_id"], inp["snapshot"], inp["expires_at"], inp["proposal"])


@runtime.activity(name="issue_refund")
async def issue_refund(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    return store.issue_refund(inp["workflow_id"], inp["booking_id"], inp["snapshot"],
                              inp["refund_cents"], inp.get("failure_at"))


@runtime.activity(name="release_seat")
async def release_seat(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    return store.release_cancelled_seat(inp["workflow_id"], inp["booking_id"], inp.get("failure_at"))


@runtime.activity(name="reverse_refund")
async def reverse_refund(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    return store.reverse_refund(inp["workflow_id"], inp["booking_id"])


@runtime.activity(name="close_cancellation")
async def close_cancellation(ctx: wf.WorkflowActivityContext, inp: dict) -> None:
    store.close_cancellation(inp["workflow_id"], inp["status"])

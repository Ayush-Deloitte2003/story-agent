import json
import os
import urllib.request
from datetime import timedelta

import dapr.ext.workflow as wf

from app.store import PostgresStore

runtime = wf.WorkflowRuntime()
store = PostgresStore()
RETRY = wf.RetryPolicy(first_retry_interval=timedelta(seconds=1), max_number_of_attempts=3, backoff_coefficient=2)


@runtime.workflow(name="flight_rebooking_workflow")
def flight_rebooking_workflow(ctx: wf.DaprWorkflowContext, inp: dict):
    workflow_id = ctx.instance_id
    _status(ctx, "getting_rebooking_recommendation", booking_id=inp["booking_id"])
    proposal = yield ctx.call_activity(recommend_rebooking, input=inp, retry_policy=RETRY)
    if not proposal["alternatives"]:
        return {"status": "no_alternatives", "booking_id": inp["booking_id"]}
    expires_at = ctx.current_utc_datetime + timedelta(seconds=inp.get("approval_timeout_seconds", 600))
    yield ctx.call_activity(
        open_rebooking_approval,
        input={"workflow_id": workflow_id, "booking_id": inp["booking_id"],
               "expires_at": expires_at.isoformat(), "proposal": proposal},
    )
    _status(ctx, "awaiting_rebooking_approval", booking_id=inp["booking_id"],
            alternatives=proposal["alternatives"], expires_at=expires_at.isoformat())
    approval = ctx.wait_for_external_event("rebooking_approval")
    timer = ctx.create_timer(expires_at)
    winner = yield wf.when_any([approval, timer])
    if winner == timer:
        yield ctx.call_activity(
            close_rebooking, input={"workflow_id": workflow_id, "status": "expired"}, retry_policy=RETRY
        )
        return {"status": "expired", "booking_id": inp["booking_id"]}

    decision = approval.get_result() or {}
    if decision.get("decision") != "approve":
        yield ctx.call_activity(
            close_rebooking, input={"workflow_id": workflow_id, "status": "rejected"}, retry_policy=RETRY
        )
        return {"status": "rejected", "booking_id": inp["booking_id"]}
    alternatives = {item["flight_id"] for item in proposal["alternatives"]}
    flight_id = decision.get("flight_id")
    if flight_id not in alternatives:
        yield ctx.call_activity(
            close_rebooking, input={"workflow_id": workflow_id, "status": "failed"}, retry_policy=RETRY
        )
        return {"status": "invalid_recommendation", "booking_id": inp["booking_id"]}
    try:
        result = yield ctx.call_activity(
            perform_rebooking,
            input={"booking_id": inp["booking_id"], "workflow_id": workflow_id, "flight_id": flight_id},
            retry_policy=RETRY,
        )
    except wf.TaskFailedError as exc:
        yield ctx.call_activity(
            close_rebooking, input={"workflow_id": workflow_id, "status": "failed"}, retry_policy=RETRY
        )
        return {"status": "rebooking_failed", "booking_id": inp["booking_id"], "error": str(exc)}
    return {"status": "rebooked", "booking_id": inp["booking_id"], "booking": result}


def _status(ctx, stage: str, **fields) -> None:
    ctx.set_custom_status(json.dumps({"stage": stage, **fields}))


@runtime.activity(name="recommend_rebooking")
async def recommend_rebooking(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    booking = store.booking(inp["booking_id"])
    if not booking or booking["status"] != "confirmed":
        raise RuntimeError("Disruption requires an existing confirmed booking")
    if not booking["ticket"] or booking["ticket"]["flight_id"] != inp["flight_id"]:
        raise RuntimeError("Disruption flight does not match the booking")
    url = os.getenv("ADVISOR_URL", "http://127.0.0.1:8100/recommend")
    request = urllib.request.Request(
        url,
        data=json.dumps({"flight_id": inp["flight_id"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        proposal = json.loads(response.read())
    return {"booking_id": inp["booking_id"], "flight_id": inp["flight_id"],
            "alternatives": proposal["alternatives"]}


@runtime.activity(name="open_rebooking_approval")
async def open_rebooking_approval(ctx: wf.WorkflowActivityContext, inp: dict) -> None:
    store.create_rebooking_approval(
        inp["workflow_id"], inp["booking_id"], inp["expires_at"], inp["proposal"]
    )


@runtime.activity(name="perform_rebooking")
async def perform_rebooking(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    return store.rebook(inp["booking_id"], inp["workflow_id"], inp["flight_id"])


@runtime.activity(name="close_rebooking")
async def close_rebooking(ctx: wf.WorkflowActivityContext, inp: dict) -> None:
    store.close_rebooking(inp["workflow_id"], inp["status"])

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager

import dapr.ext.workflow as wf
from fastapi import FastAPI, HTTPException

from app.disruption_workflow import flight_rebooking_workflow, runtime

client: wf.DaprWorkflowClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    runtime.start()
    client = wf.DaprWorkflowClient()
    yield
    runtime.shutdown()


app = FastAPI(title="Flight disruption workflow worker", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/internal/workflows/{workflow_id}")
async def workflow_state(workflow_id: str):
    state = await asyncio.to_thread(client.get_workflow_state, workflow_id, fetch_payloads=True)
    if state is None:
        raise HTTPException(404, "Workflow not found")
    return {
        "workflow_id": workflow_id,
        "runtime_status": state.runtime_status.name,
        "custom_status": state.serialized_custom_status,
        "output": state.serialized_output,
        "failure": getattr(state.failure_details, "message", None),
    }


@app.get("/dapr/subscribe")
def subscriptions():
    return [{"pubsubname": "pubsub", "topic": "flight-disruption",
             "route": "/events/flight-disruption"}]


@app.post("/events/flight-disruption")
async def flight_disruption(event: dict):
    data = event.get("data") or {}
    event_id = event.get("id")
    source = event.get("source")
    booking_id = data.get("booking_id")
    flight_id = data.get("flight_id")
    if not all(isinstance(value, str) and value for value in (event_id, source, booking_id, flight_id)):
        return {"status": "DROP"}
    workflow_id = "disruption-" + hashlib.sha256(f"{source}:{event_id}".encode()).hexdigest()[:40]
    try:
        state = await asyncio.to_thread(client.get_workflow_state, workflow_id, fetch_payloads=False)
        if state is None:
            await asyncio.to_thread(
                client.schedule_new_workflow, flight_rebooking_workflow,
                input={"booking_id": booking_id, "flight_id": flight_id},
                instance_id=workflow_id,
            )
    except Exception:
        state = await asyncio.to_thread(client.get_workflow_state, workflow_id, fetch_payloads=False)
        if state is None:
            return {"status": "RETRY"}
    return {"status": "SUCCESS"}


@app.post("/internal/approval/raise")
async def raise_rebooking_approval(event: dict):
    await asyncio.to_thread(
        client.raise_workflow_event, event["workflow_id"], "rebooking_approval", data=event["decision"]
    )
    return {"raised": True}

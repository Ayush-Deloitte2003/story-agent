import asyncio
import json
from contextlib import asynccontextmanager

import dapr.ext.workflow as wf
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

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


async def _schedule(workflow_id: str, booking_id: str, flight_id: str) -> bool | None:
    """Start the workflow once. Returns whether it already existed; None means retry."""
    existed = True
    try:
        state = await asyncio.to_thread(client.get_workflow_state, workflow_id, fetch_payloads=False)
        if state is None:
            existed = False
            await asyncio.to_thread(
                client.schedule_new_workflow, flight_rebooking_workflow,
                input={"booking_id": booking_id, "flight_id": flight_id},
                instance_id=workflow_id,
            )
    except Exception:
        state = await asyncio.to_thread(client.get_workflow_state, workflow_id, fetch_payloads=False)
        if state is None:
            return None
        existed = True
    return existed


class DispatchEvent(BaseModel):
    workflow_id: str
    booking_id: str
    flight_id: str
    event_id: str | None = None
    source: str | None = None


@app.post("/internal/disruptions/schedule")
async def schedule_disruption(event: DispatchEvent):
    existed = await _schedule(event.workflow_id, event.booking_id, event.flight_id)
    if existed is None:
        raise HTTPException(503, "Workflow could not be scheduled")
    return {"workflow_id": event.workflow_id, "scheduled": True, "already_existed": existed}


@app.post("/internal/approval/raise")
async def raise_rebooking_approval(event: dict):
    await asyncio.to_thread(
        client.raise_workflow_event, event["workflow_id"], "rebooking_approval", data=event["decision"]
    )
    return {"raised": True}

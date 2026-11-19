import asyncio
from contextlib import asynccontextmanager

import dapr.ext.workflow as wf
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.cancellation_workflow import flight_cancellation_workflow, runtime
from app.store import PostgresStore

store = PostgresStore()
client: wf.DaprWorkflowClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    runtime.start()
    client = wf.DaprWorkflowClient()
    yield
    runtime.shutdown()


app = FastAPI(title="Flight cancellation workflow worker", lifespan=lifespan)


class ScheduleRequest(BaseModel):
    workflow_id: str
    booking_id: str
    request: dict
    snapshot: dict


class DecisionEvent(BaseModel):
    workflow_id: str
    decision: dict


@app.get("/healthz")
def healthz():
    store.ping()
    return {"status": "ok"}


@app.post("/internal/cancellations/schedule")
async def schedule(request: ScheduleRequest):
    existed = True
    try:
        state = await asyncio.to_thread(client.get_workflow_state, request.workflow_id, fetch_payloads=False)
        if state is None:
            existed = False
            await asyncio.to_thread(
                client.schedule_new_workflow, flight_cancellation_workflow,
                input={"booking_id": request.booking_id, "request": request.request,
                       "snapshot": request.snapshot},
                instance_id=request.workflow_id,
            )
    except Exception:
        state = await asyncio.to_thread(client.get_workflow_state, request.workflow_id, fetch_payloads=False)
        if state is None:
            raise
        existed = True
    return {"workflow_id": request.workflow_id, "scheduled": True, "already_existed": existed}


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


@app.post("/internal/approval/raise")
async def raise_approval(event: DecisionEvent):
    await asyncio.to_thread(
        client.raise_workflow_event, event.workflow_id, "approval", data=event.decision
    )
    return {"raised": True}

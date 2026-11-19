import asyncio
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException

from app.contracts import BookingRequest, DecisionRequest, DisruptionEvent, booking_payload
from app.store import PostgresStore

store = PostgresStore()
DAPR_PORT = os.getenv("DAPR_HTTP_PORT", "3500")
FAILURE_INJECTION_ENABLED = os.getenv("ENABLE_FAILURE_INJECTION", "false").lower() == "true"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(store.initialize)
    yield


app = FastAPI(title="Flight booking API (simulated)", lifespan=lifespan)


def _invoke(app_id: str, method: str, body: dict | None = None, http_method: str = "POST"):
    request = urllib.request.Request(
        f"http://127.0.0.1:{DAPR_PORT}/v1.0/invoke/{app_id}/method/{method}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=http_method,
    )
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code not in (502, 503, 504) or attempt == 5:
                raise HTTPException(503, f"Workflow service {app_id} is unavailable") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 5:
                raise HTTPException(503, f"Workflow service {app_id} is unavailable") from exc
        except json.JSONDecodeError as exc:
            raise HTTPException(502, f"Workflow service {app_id} returned invalid JSON") from exc
        time.sleep(min(0.5 * (2 ** attempt), 2))
    raise HTTPException(503, f"Workflow service {app_id} is unavailable")


def _publish_disruption(event: dict) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{DAPR_PORT}/v1.0/publish/pubsub/flight-disruption",
        data=json.dumps(event).encode(),
        headers={"Content-Type": "application/cloudevents+json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status not in (200, 202, 204):
                raise HTTPException(503, "Dapr Pub/Sub did not accept the disruption event")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(503, "Dapr Pub/Sub is unavailable") from exc


@app.get("/healthz")
def healthz():
    store.ping()
    return {"status": "ok"}


@app.get("/flights")
def flights():
    from app.inventory import FLIGHTS
    return {"flights": FLIGHTS, "simulated": True}


@app.post("/bookings", status_code=202)
def create_booking(
    request: BookingRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
):
    try:
        payload = booking_payload(request, FAILURE_INJECTION_ENABLED)
        workflow_id = "booking-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:40]
        booking, _created = store.create_booking(workflow_id, idempotency_key, payload)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _invoke("booking-worker", "internal/bookings/schedule", {
        "workflow_id": workflow_id, "booking_id": workflow_id, "request": payload,
    })
    return {"booking_id": booking["booking_id"], "workflow_id": workflow_id, "status": booking["status"]}


@app.get("/bookings/{booking_id}")
def get_booking(booking_id: str):
    booking = store.booking(booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found")
    workflow = _invoke("booking-worker", f"internal/workflows/{booking_id}", http_method="GET")
    return {"booking": booking, "workflow": workflow}


@app.get("/inbox")
def inbox():
    return {"items": store.inbox()}


@app.get("/inbox/{workflow_id}")
def inbox_detail(workflow_id: str):
    item = store.inbox_item(workflow_id)
    if item is None:
        raise HTTPException(404, "Approval not found")
    worker = "booking-worker" if item["kind"] == "booking" else "disruption-worker"
    workflow = _invoke(worker, f"internal/workflows/{workflow_id}", http_method="GET")
    return {"item": item, "workflow": workflow}


@app.post("/inbox/{workflow_id}/decision")
def decide(workflow_id: str, request: DecisionRequest):
    item = store.inbox_item(workflow_id)
    if item is None:
        raise HTTPException(404, "Approval not found")
    decision = request.model_dump(exclude_none=True)
    if request.decision == "approve" and item["kind"] == "rebooking":
        alternatives = {option["flight_id"] for option in item["proposal"]["alternatives"]}
        if request.flight_id not in alternatives:
            raise HTTPException(422, "Choose one of the proposed alternative flights")
    elif request.flight_id is not None:
        raise HTTPException(422, "flight_id is only valid for rebooking approvals")
    if not store.decide(workflow_id, decision):
        raise HTTPException(409, "Approval already has a different decision")
    worker = "booking-worker" if item["kind"] == "booking" else "disruption-worker"
    _invoke(worker, "internal/approval/raise", {"workflow_id": workflow_id, "decision": decision})
    return {"workflow_id": workflow_id, "status": "decision_recorded"}


@app.post("/disruptions", status_code=202)
def submit_disruption(event: DisruptionEvent):
    booking = store.booking(event.booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found")
    if booking["status"] != "confirmed" or not booking["ticket"] or booking["ticket"]["flight_id"] != event.flight_id:
        raise HTTPException(409, "Disruption must target the booking's current confirmed flight")
    workflow_id = "disruption-" + hashlib.sha256(f"{event.source}:{event.id}".encode()).hexdigest()[:40]
    cloud_event = {
        "specversion": "1.0", "id": event.id, "source": event.source,
        "type": "com.example.flight.disrupted",
        "data": {"booking_id": event.booking_id, "flight_id": event.flight_id},
    }
    _publish_disruption(cloud_event)
    return {"workflow_id": workflow_id, "event_id": event.id, "status": "accepted"}

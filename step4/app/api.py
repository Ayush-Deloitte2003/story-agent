import asyncio
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from app import dispatcher, feed_poller
from app.contracts import (
    CANCELLATION_ONLY_FIELDS, BookingRequest, CancellationRequest, DecisionRequest, booking_payload,
    cancellation_payload,
)
from app.events import Accepted, classify
from app.store import PostgresStore

store = PostgresStore()
UI_PAGE = Path(__file__).parent / "static" / "index.html"
DAPR_PORT = os.getenv("DAPR_HTTP_PORT", "3500")
FAILURE_INJECTION_ENABLED = os.getenv("ENABLE_FAILURE_INJECTION", "false").lower() == "true"
DISPATCH_INTERVAL_SECONDS = float(os.getenv("DISPATCH_INTERVAL_SECONDS", "2"))


async def reconcile_forever() -> None:
    while True:
        try:
            await asyncio.to_thread(dispatcher.dispatch_due, store, _dispatch_invoke)
        except Exception as exc:
            logging.getLogger("step4.dispatcher").warning("reconcile pass failed: %s", type(exc).__name__)
        await asyncio.sleep(DISPATCH_INTERVAL_SECONDS)


async def poll_feed_forever() -> None:
    interval = float(os.getenv("OPS_FEED_POLL_INTERVAL_SECONDS", "5"))
    batch_size = int(os.getenv("OPS_FEED_BATCH_SIZE", "20"))
    max_events = int(os.getenv("OPS_FEED_MAX_EVENTS_PER_PASS", "200"))
    await asyncio.to_thread(store.ensure_cursor, feed_poller.CURSOR_NAME)
    while True:
        try:
            await asyncio.to_thread(feed_poller.poll_pass, store, batch_size, max_events)
        except Exception:
            pass  # already counted and logged by poll_pass
        await asyncio.sleep(interval)


def feed_polling_enabled() -> bool:
    return os.getenv("OPS_FEED_POLL_ENABLED", "false").lower() == "true"


def feed_simulation_enabled() -> bool:
    return os.getenv("OPS_FEED_SIMULATION", "false").lower() == "true"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(store.initialize)
    reconciler = asyncio.create_task(reconcile_forever())
    poller = asyncio.create_task(poll_feed_forever()) if feed_polling_enabled() else None
    try:
        yield
    finally:
        reconciler.cancel()
        if poller:
            poller.cancel()


app = FastAPI(title="Flight booking API (simulated)", lifespan=lifespan)


def _dispatch_invoke(app_id: str, method: str, body: dict):
    return _invoke(app_id, method, body, attempts=2)


def _try_dispatch(dispatch_id: str) -> None:
    """Best-effort fast path; the leased reconciler guarantees eventual delivery."""
    try:
        dispatcher.dispatch_due(store, _dispatch_invoke, limit=1, dispatch_id=dispatch_id)
    except Exception as exc:
        logging.getLogger("step4.dispatcher").warning("fast dispatch failed: %s", type(exc).__name__)

def _invoke(app_id: str, method: str, body: dict | None = None, http_method: str = "POST",
            attempts: int = 6):
    request = urllib.request.Request(
        f"http://127.0.0.1:{DAPR_PORT}/v1.0/invoke/{app_id}/method/{method}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=http_method,
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code not in (502, 503, 504) or attempt == attempts - 1:
                raise HTTPException(503, f"Workflow service {app_id} is unavailable") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == attempts - 1:
                raise HTTPException(503, f"Workflow service {app_id} is unavailable") from exc
        except json.JSONDecodeError as exc:
            raise HTTPException(502, f"Workflow service {app_id} returned invalid JSON") from exc
        time.sleep(min(0.5 * (2 ** attempt), 2))
    raise HTTPException(503, f"Workflow service {app_id} is unavailable")


@app.get("/healthz")
def healthz():
    store.ping()
    return {"status": "ok"}


@app.get("/ui", response_class=HTMLResponse)
def reviewer_ui():
    """Static reviewer page; contains no data and calls only existing endpoints."""
    return HTMLResponse(UI_PAGE.read_text(encoding="utf-8"))


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
    # The booking and its dispatch record are committed; delivery failures are retried by the reconciler.
    _try_dispatch(workflow_id)
    return {"booking_id": booking["booking_id"], "workflow_id": workflow_id, "status": booking["status"]}


@app.get("/bookings/{booking_id}")
def get_booking(booking_id: str):
    booking = store.booking(booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found")
    try:
        workflow = _invoke("booking-worker", f"internal/workflows/{booking_id}", http_method="GET")
    except HTTPException:
        dispatch = store.dispatch_record(booking_id)
        if dispatch is None or dispatch["status"] == "dispatched":
            raise
        workflow = {"runtime_status": "QUEUED", "dispatch": dispatch}
    return {"booking": booking, "workflow": workflow}


@app.post("/cancellations", status_code=202)
def create_cancellation(
    request: CancellationRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
):
    try:
        payload = cancellation_payload(request, FAILURE_INJECTION_ENABLED)
        workflow_id = "cancellation-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:40]
        row, _created = store.create_cancellation(workflow_id, idempotency_key, payload)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    # The cancellation and its dispatch record are committed; delivery failures are retried by the reconciler.
    _try_dispatch(workflow_id)
    return {"cancellation_id": workflow_id, "workflow_id": workflow_id,
            "booking_id": row["booking_id"], "status": row["status"]}


@app.get("/inbox")
def inbox():
    return {"items": store.inbox()}


def _worker_for(kind: str) -> str:
    return {"booking": "booking-worker", "cancellation": "cancellation-worker"}.get(kind, "disruption-worker")


@app.get("/inbox/{workflow_id}")
def inbox_detail(workflow_id: str):
    item = store.inbox_item(workflow_id)
    if item is None:
        raise HTTPException(404, "Approval not found")
    worker = _worker_for(item["kind"])
    workflow = _invoke(worker, f"internal/workflows/{workflow_id}", http_method="GET")
    return {"item": item, "workflow": workflow}


@app.post("/inbox/{workflow_id}/decision")
def decide(workflow_id: str, request: DecisionRequest):
    item = store.inbox_item(workflow_id)
    if item is None:
        raise HTTPException(404, "Approval not found")
    decision = request.model_dump(exclude_none=True)
    if item["kind"] == "cancellation":
        if request.flight_id is not None:
            raise HTTPException(422, "flight_id is only valid for rebooking approvals")
        if request.decision == "approve":
            if request.request_hash is None or request.refund_cents is None:
                raise HTTPException(422, "Approving a cancellation requires request_hash and refund_cents")
            if (request.request_hash != item["proposal"]["request_hash"]
                    or request.refund_cents != item["proposal"]["refund_cents"]):
                raise HTTPException(409, "Approval does not match this cancellation request and refund amount")
        else:
            for field in CANCELLATION_ONLY_FIELDS:
                decision.pop(field, None)
    else:
        for field in CANCELLATION_ONLY_FIELDS:
            decision.pop(field, None)
        if request.decision == "approve" and item["kind"] == "rebooking":
            alternatives = {option["flight_id"] for option in item["proposal"]["alternatives"]}
            if request.flight_id not in alternatives:
                raise HTTPException(422, "Choose one of the proposed alternative flights")
        elif request.flight_id is not None:
            raise HTTPException(422, "flight_id is only valid for rebooking approvals")
    if not store.decide(workflow_id, decision):
        raise HTTPException(409, "Approval already has a different decision")
    worker = _worker_for(item["kind"])
    _invoke(worker, "internal/approval/raise", {"workflow_id": workflow_id, "decision": decision})
    return {"workflow_id": workflow_id, "status": "decision_recorded"}




@app.post("/simulation/ops-feed", status_code=202)
def seed_feed_event(raw: dict = Body(...)):
    """Insert a simulated operations-feed row; disabled unless OPS_FEED_SIMULATION=true."""
    if not feed_simulation_enabled():
        raise HTTPException(404, "Not found")
    source_event_id = raw.get("source_event_id")
    if (not isinstance(source_event_id, str) or not source_event_id.strip()
            or len(source_event_id) > feed_poller.MAX_SOURCE_EVENT_ID):
        raise HTTPException(422, "source_event_id must be a non-empty string up to 128 characters")
    payload = {key: value for key, value in raw.items() if key != "source_event_id"}
    created = store.insert_feed_event(source_event_id, payload)
    return {"source_event_id": source_event_id, "status": "seeded" if created else "duplicate"}


@app.get("/ops-feed/status")
def feed_status():
    return {"enabled": feed_polling_enabled(), "cursor": store.cursor(feed_poller.CURSOR_NAME),
            "counters": feed_poller.counters.totals}


@app.post("/disruptions", status_code=202)
def submit_disruption(raw: dict = Body(...)):
    outcome = classify(raw)
    if not isinstance(outcome, Accepted):
        store.quarantine_event(outcome, raw)
        return {"status": "quarantined", "reason": outcome.reason, "quarantine_id": outcome.quarantine_id}
    data = outcome.data
    if store.receipt(outcome.source, outcome.event_id) is None:
        booking = store.booking(data["booking_id"])
        if not booking:
            raise HTTPException(404, "Booking not found")
        if (booking["status"] != "confirmed" or not booking["ticket"]
                or booking["ticket"]["flight_id"] != data["flight_id"]):
            raise HTTPException(409, "Disruption must target the booking's current confirmed flight")
    try:
        workflow_id, created = store.accept_event(outcome)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if created:
        _try_dispatch(workflow_id)
    return {"workflow_id": workflow_id, "event_id": outcome.event_id,
            "status": "accepted" if created else "duplicate"}

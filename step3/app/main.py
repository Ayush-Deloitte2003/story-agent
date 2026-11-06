"""FastAPI front door for the story agent, backed by a Dapr workflow.

POST /chat                       start a story workflow; returns the story awaiting approval
POST /chat/{thread_id}/decision  approve or reject the pending story
GET  /chat/{thread_id}           current status of a workflow
GET  /                           minimal page to try the flow in a browser

`thread_id` is the Dapr workflow instance id. All state (the story, the
approval wait, publish retries) lives in Dapr, so this process can restart
at any time without losing a story that is waiting for approval.
"""

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import dapr.ext.workflow as wf  # noqa: E402
from fastapi import Body, FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app.agent import mcp_url  # noqa: E402
from app.approval import DaprDecisionClaims, approval_event_name  # noqa: E402
from app.events import (  # noqa: E402
    parse_story_quality_review_event,
    parse_story_requested_event,
    workflow_id_for_event,
    workflow_id_for_quality_review_event,
)
from app.workflow import (  # noqa: E402
    runtime,
    story_quality_review_workflow,
    story_workflow,
)
from app.workflow_status import workflow_view  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("story-agent")
for noisy in ("LiteLLM", "mcp", "httpx"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
for noisy in ("WorkflowRuntime", "durabletask-worker", "durabletask-client"):
    logging.getLogger(noisy).setLevel(logging.ERROR)  # app/workflow.py logs retries itself

# Default approval window; a request can override it (handy for testing).
APPROVAL_TIMEOUT_HOURS = float(os.getenv("APPROVAL_TIMEOUT_HOURS", "24"))
# How long an HTTP call waits for the workflow to reach the next stage.
WAIT_SECONDS = float(os.getenv("WAIT_SECONDS", "120"))

client: wf.DaprWorkflowClient | None = None
decision_claims = DaprDecisionClaims()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    runtime.start()
    client = wf.DaprWorkflowClient()
    log.info("Dapr workflow runtime started; publisher MCP server at %s", mcp_url())
    yield
    runtime.shutdown()


app = FastAPI(title="Story agent: step 3 (Dapr)", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str
    approval_timeout_seconds: float | None = Field(
        None, gt=0, description="Override the approval window for this story (default from APPROVAL_TIMEOUT_HOURS)."
    )


class StoryRequestedWebhook(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=512)
    message: str = Field(min_length=1)
    approval_timeout_seconds: float | None = Field(None, gt=0)


class StoryQualityReviewWebhook(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=500)
    story: str = Field(min_length=1, max_length=20000)
    request: str = Field("", max_length=2000)


class StoryContinuityCheckWebhook(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=500)
    story: str = Field(min_length=1, max_length=20000)
    request: str = Field("", max_length=2000)


class StoryBriefComplianceWebhook(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=500)
    story: str = Field(min_length=1, max_length=20000)
    request: str = Field("", max_length=2000)


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    feedback: str | None = None
    expected_revision: int | None = Field(None, ge=0)


def _view(thread_id: str) -> dict | None:
    return workflow_view(client, thread_id)


async def _wait_for(thread_id: str, done) -> dict:
    """Poll the workflow until `done(view)` is true or WAIT_SECONDS pass."""
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        view = await asyncio.to_thread(_view, thread_id)
        if view and (done(view) or time.monotonic() > deadline):
            return view
        await asyncio.sleep(0.5)


@app.post("/chat")
async def chat(req: ChatRequest):
    timeout = req.approval_timeout_seconds or APPROVAL_TIMEOUT_HOURS * 3600
    thread_id = str(uuid.uuid4())
    await asyncio.to_thread(
        client.schedule_new_workflow,
        story_workflow,
        input={"message": req.message, "approval_timeout_seconds": timeout},
        instance_id=thread_id,
    )
    log.info("Started story workflow %s (approval window %.0fs)", thread_id, timeout)
    return await _wait_for(thread_id, lambda v: v["status"] not in ("writing",))


def _publish_cloud_event(event: dict) -> None:
    dapr_port = os.getenv("DAPR_HTTP_PORT", "3500")
    request = urllib.request.Request(
        f"http://127.0.0.1:{dapr_port}/v1.0/publish/pubsub/story-events",
        data=json.dumps(event).encode("utf-8"),
        headers={"Content-Type": "application/cloudevents+json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status not in (200, 202, 204):
                raise RuntimeError(f"Dapr Pub/Sub returned HTTP {response.status}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not publish event through the Dapr sidecar: {exc}") from exc


@app.post("/webhooks/story-requested", status_code=202, include_in_schema=False)
async def webhook_story_requested(req: StoryRequestedWebhook):
    event = {
        "specversion": "1.0",
        "id": req.id,
        "source": req.source,
        "type": "story.requested",
        "datacontenttype": "application/json",
        "data": {"message": req.message},
    }
    if req.approval_timeout_seconds is not None:
        event["data"]["approval_timeout_seconds"] = req.approval_timeout_seconds
    try:
        parse_story_requested_event(event)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    try:
        await asyncio.to_thread(_publish_cloud_event, event)
    except RuntimeError as exc:
        log.exception("Webhook event %s/%s could not be published", req.source, req.id)
        raise HTTPException(503, str(exc)) from exc
    return {"status": "ACCEPTED", "event_id": req.id, "source": req.source}


@app.post("/webhooks/story-quality-review-requested", status_code=202, include_in_schema=False)
async def webhook_story_quality_review_requested(req: StoryQualityReviewWebhook):
    event = {
        "specversion": "1.0",
        "id": req.id,
        "source": req.source,
        "type": "story.quality-review.requested",
        "datacontenttype": "application/json",
        "data": {"title": req.title, "story": req.story, "request": req.request},
    }
    try:
        parse_story_quality_review_event(event)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    try:
        await asyncio.to_thread(_publish_cloud_event, event)
    except RuntimeError as exc:
        log.exception("Quality-review event %s/%s could not be published", req.source, req.id)
        raise HTTPException(503, str(exc)) from exc
    return {"status": "ACCEPTED", "event_id": req.id, "source": req.source}


@app.post("/webhooks/story-continuity-check-requested", status_code=202, include_in_schema=False)
async def webhook_story_continuity_check_requested(req: StoryContinuityCheckWebhook):
    event = {
        "specversion": "1.0",
        "id": req.id,
        "source": req.source,
        "type": "story.continuity-check.requested",
        "datacontenttype": "application/json",
        "data": {"title": req.title, "story": req.story, "request": req.request},
    }
    try:
        parse_story_continuity_check_event(event)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    try:
        await asyncio.to_thread(_publish_cloud_event, event)
    except RuntimeError as exc:
        log.exception("Continuity-check event %s/%s could not be published", req.source, req.id)
        raise HTTPException(503, str(exc)) from exc
    return {"status": "ACCEPTED", "event_id": req.id, "source": req.source}


@app.post("/webhooks/story-brief-compliance-requested", status_code=202, include_in_schema=False)
async def webhook_story_brief_compliance_requested(req: StoryBriefComplianceWebhook):
    event = {
        "specversion": "1.0",
        "id": req.id,
        "source": req.source,
        "type": "story.brief-compliance.requested",
        "datacontenttype": "application/json",
        "data": {"title": req.title, "story": req.story, "request": req.request},
    }
    try:
        parse_story_brief_compliance_event(event)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    try:
        await asyncio.to_thread(_publish_cloud_event, event)
    except RuntimeError as exc:
        log.exception("Brief-compliance event %s/%s could not be published", req.source, req.id)
        raise HTTPException(503, str(exc)) from exc
    return {"status": "ACCEPTED", "event_id": req.id, "source": req.source}


@app.get("/dapr/subscribe", include_in_schema=False)
async def dapr_subscriptions():
    return [{
        "pubsubname": "pubsub",
        "topic": "story-events",
        "routes": {
            "rules": [
                {
                    "match": 'event.type == "story.requested"',
                    "path": "/events/story-requested",
                },
                {
                    "match": 'event.type == "story.quality-review.requested"',
                    "path": "/events/story-quality-review-requested",
                },
            ],
            "default": "/events/unhandled",
        },
        "deadLetterTopic": "story-events-dead",
    }]


@app.post("/events/story-requested", include_in_schema=False)
async def story_requested(event: object = Body(...)):
    try:
        source, event_id, workflow_input = parse_story_requested_event(event)
    except ValueError as exc:
        log.warning("Dropping invalid story event: %s", exc)
        return {"status": "DROP", "message": str(exc)}

    thread_id = workflow_id_for_event(source, event_id)
    workflow_input.setdefault("approval_timeout_seconds", APPROVAL_TIMEOUT_HOURS * 3600)
    try:
        await asyncio.to_thread(
            client.schedule_new_workflow,
            story_workflow,
            input=workflow_input,
            instance_id=thread_id,
        )
    except Exception:
        existing = await asyncio.to_thread(client.get_workflow_state, thread_id, fetch_payloads=False)
        if existing is None:
            log.exception("Could not start workflow for event %s/%s", source, event_id)
            return {"status": "RETRY"}
        log.info("Acknowledging redelivered event %s/%s as workflow %s", source, event_id, thread_id)
    else:
        log.info("Started workflow %s from event %s/%s", thread_id, source, event_id)

    return {"status": "SUCCESS", "thread_id": thread_id}


@app.post("/events/story-quality-review-requested", include_in_schema=False)
async def story_quality_review_requested(event: object = Body(...)):
    try:
        source, event_id, workflow_input = parse_story_quality_review_event(event)
    except ValueError as exc:
        log.warning("Dropping invalid quality-review event: %s", exc)
        return {"status": "DROP", "message": str(exc)}

    workflow_id = workflow_id_for_quality_review_event(source, event_id)
    try:
        await asyncio.to_thread(
            client.schedule_new_workflow,
            story_quality_review_workflow,
            input=workflow_input,
            instance_id=workflow_id,
        )
    except Exception:
        existing = await asyncio.to_thread(client.get_workflow_state, workflow_id, fetch_payloads=False)
        if existing is None:
            log.exception("Could not start quality-review workflow for event %s/%s", source, event_id)
            return {"status": "RETRY"}
        log.info("Acknowledging redelivered quality-review event %s/%s as workflow %s",
                 source, event_id, workflow_id)
    else:
        log.info("Started quality-review workflow %s from event %s/%s", workflow_id, source, event_id)

    return {"status": "SUCCESS", "thread_id": workflow_id}


@app.post("/events/unhandled", include_in_schema=False)
async def unhandled_event(event: object = Body(...)):
    event_type = event.get("type") if isinstance(event, dict) else None
    log.warning("Dropping event with no matching story route: %s", event_type)
    return {"status": "DROP"}


@app.get("/events/{event_id}", include_in_schema=False)
async def event_status(event_id: str, source: str):
    thread_id = workflow_id_for_event(source, event_id)
    view = await asyncio.to_thread(_view, thread_id)
    if view is None:
        raise HTTPException(404, "No workflow found for this event.")
    return view


@app.get("/events/quality-review/{event_id}", include_in_schema=False)
async def quality_review_event_status(event_id: str, source: str):
    workflow_id = workflow_id_for_quality_review_event(source, event_id)
    view = await asyncio.to_thread(_view, workflow_id)
    if view is None:
        raise HTTPException(404, "No quality-review workflow found for this event.")
    return view


def _specialist_status(worker: str, event_id: str, source: str) -> dict | None:
    query = urllib.parse.urlencode({"source": source})
    request = urllib.request.Request(
        f"http://{worker}:8001/events/{event_id}?{query}",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise HTTPException(503, f"Specialist status service returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(503, f"Specialist status service is unavailable: {exc}") from exc


@app.get("/events/continuity-check/{event_id}", include_in_schema=False)
async def continuity_check_event_status(event_id: str, source: str):
    view = await asyncio.to_thread(
        _specialist_status, "continuity-worker", event_id, source
    )
    if view is None:
        raise HTTPException(404, "No continuity-check workflow found for this event.")
    return view


@app.get("/events/brief-compliance/{event_id}", include_in_schema=False)
async def brief_compliance_event_status(event_id: str, source: str):
    view = await asyncio.to_thread(
        _specialist_status, "brief-compliance-worker", event_id, source
    )
    if view is None:
        raise HTTPException(404, "No brief-compliance workflow found for this event.")
    return view


@app.post("/chat/{thread_id}/decision")
async def decide(thread_id: str, req: DecisionRequest):
    current = await asyncio.to_thread(_view, thread_id)
    if current is None:
        raise HTTPException(404, f"No run found for thread_id {thread_id}")
    revision = current.get("revision", 0)
    if current["status"] == "awaiting_approval":
        expires = datetime.fromisoformat(current["approval_expires_at"])
        if datetime.now(timezone.utc) > expires:
            raise HTTPException(410, "The approval window for this story has expired.")
        if req.expected_revision is not None and req.expected_revision != revision:
            raise HTTPException(409, "This draft has changed. Refresh before deciding.")
    elif current["status"] == "expired":
        raise HTTPException(410, "The approval window for this story has expired.")
    else:
        raise HTTPException(409, "Nothing is awaiting approval on this thread.")

    try:
        claimed = await asyncio.to_thread(
            decision_claims.claim,
            thread_id,
            revision,
            req.decision,
            req.feedback,
        )
    except RuntimeError as exc:
        log.exception("Could not claim decision for workflow %s revision %d", thread_id, revision)
        raise HTTPException(503, "Decision coordination is temporarily unavailable.") from exc
    if not claimed:
        raise HTTPException(409, "A different decision for this draft is already being processed.")

    log.info("Human decision on %s revision %d: %s", thread_id, revision, req.decision)
    try:
        await asyncio.to_thread(
            client.raise_workflow_event, thread_id, approval_event_name(revision),
            data={"decision": req.decision, "feedback": req.feedback},
        )
    except Exception as exc:
        log.exception("Could not submit decision event for workflow %s revision %d", thread_id, revision)
        raise HTTPException(
            503, "Could not submit the decision event; retry the same decision."
        ) from exc

    if req.decision == "reject":
        # Wait for the revised draft (or an end state).
        result = await _wait_for(
            thread_id,
            lambda v: (v["status"] == "awaiting_approval" and v.get("revision", 0) > revision)
            or v["status"] in ("published", "failed", "expired", "terminated"),
        )
    else:
        # Approved: return as soon as publishing has started; poll GET for the result.
        result = await _wait_for(thread_id, lambda v: v["status"] != "awaiting_approval")
    return result


@app.get("/chat/{thread_id}")
async def status(thread_id: str):
    view = await asyncio.to_thread(_view, thread_id)
    if view is None:
        raise HTTPException(404, f"No run found for thread_id {thread_id}")
    return view


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(Path(__file__).parent / "index.html")

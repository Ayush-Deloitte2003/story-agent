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
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import dapr.ext.workflow as wf  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app.agent import mcp_url  # noqa: E402
from app.approval import DecisionClaims, approval_event_name  # noqa: E402
from app.workflow import runtime, story_workflow  # noqa: E402

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
decision_claims = DecisionClaims()
decision_submission_lock = asyncio.Lock()


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


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    feedback: str | None = None
    expected_revision: int | None = Field(None, ge=0)


def _loads(raw):
    """Dapr returns JSON; a custom status string arrives JSON-encoded once more."""
    value = json.loads(raw) if raw else None
    return json.loads(value) if isinstance(value, str) else value


def _view(thread_id: str) -> dict | None:
    state = client.get_workflow_state(thread_id, fetch_payloads=True)
    if state is None:
        return None
    custom = _loads(state.serialized_custom_status) or {}
    runtime_status = state.runtime_status
    body: dict = {"thread_id": thread_id, "runtime_status": runtime_status.name}

    if runtime_status in (wf.WorkflowStatus.RUNNING, wf.WorkflowStatus.PENDING):
        body["status"] = custom.get("stage", "writing")
        for key in ("story", "revision", "approval_expires_at"):
            if key in custom:
                body[key] = custom[key]
    elif runtime_status == wf.WorkflowStatus.COMPLETED:
        output = _loads(state.serialized_output) or {}
        body.update(output)
        if output.get("status") == "published":
            body["reply"] = output.get("result")
    elif runtime_status == wf.WorkflowStatus.FAILED:
        details = state.failure_details
        body.update(status="failed", error=getattr(details, "message", "workflow failed"))
    else:
        body["status"] = runtime_status.name.lower()

    if isinstance(body.get("story"), dict):
        body["llm_calls"] = body["story"].pop("llm_calls", None)
    expires = body.get("approval_expires_at")
    if expires and not expires.endswith(("Z", "+00:00")):
        body["approval_expires_at"] = expires + "+00:00"  # Dapr's clock is UTC
    return body


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


@app.post("/chat/{thread_id}/decision")
async def decide(thread_id: str, req: DecisionRequest):
    async with decision_submission_lock:
        current = await asyncio.to_thread(_view, thread_id)
        if current is None:
            raise HTTPException(404, f"No run found for thread_id {thread_id}")
        revision = current.get("revision", 0)
        decision_claims.reconcile(
            thread_id,
            revision if current["status"] == "awaiting_approval" else None,
        )
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

        if not decision_claims.claim(thread_id, revision):
            raise HTTPException(409, "A decision for this draft is already being processed.")

        log.info("Human decision on %s: %s", thread_id, req.decision)
        await asyncio.to_thread(
            client.raise_workflow_event, thread_id, approval_event_name(revision),
            data={"decision": req.decision, "feedback": req.feedback},
        )

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
    decision_claims.reconcile(
        thread_id,
        result.get("revision", 0) if result["status"] == "awaiting_approval" else None,
    )
    return result


@app.get("/chat/{thread_id}")
async def status(thread_id: str):
    view = await asyncio.to_thread(_view, thread_id)
    if view is None:
        raise HTTPException(404, f"No run found for thread_id {thread_id}")
    decision_claims.reconcile(
        thread_id,
        view.get("revision", 0) if view["status"] == "awaiting_approval" else None,
    )
    return view


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(Path(__file__).parent / "index.html")

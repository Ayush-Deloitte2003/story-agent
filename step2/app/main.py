"""FastAPI front door for the story agent.

POST /chat                       start a run; returns the story awaiting approval
POST /chat/{thread_id}/decision  approve or reject the pending story
GET  /chat/{thread_id}           current status of a run
GET  /                           minimal page to try the flow in a browser
"""

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from langgraph.types import Command  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.agent import PUBLISH_TOOL, build_agent, load_mcp_tools, mcp_url  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("story-agent")
for noisy in ("LiteLLM", "mcp", "httpx"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# How long a story may wait for a human decision before it expires.
APPROVAL_TIMEOUT = timedelta(hours=float(os.getenv("APPROVAL_TIMEOUT_HOURS", "24")))

agent = None  # built at startup, once the MCP tools are loaded


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent
    tools = await load_mcp_tools()
    log.info("Loaded MCP tools from %s: %s", mcp_url(), ", ".join(t.name for t in tools))
    agent = build_agent(tools)
    yield


app = FastAPI(title="Story agent: step 2", lifespan=lifespan)

# thread_id -> when the pending approval expires. In memory, like the
# checkpointer; step 3 makes both durable.
approval_deadlines: dict[str, datetime] = {}
# thread_id -> the error that stopped the run (e.g. the MCP tool failing).
run_errors: dict[str, str] = {}


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    feedback: str | None = None


class LLMCallCounter(BaseCallbackHandler):
    """Counts and logs every LLM call made while serving one request."""

    def __init__(self) -> None:
        self.count = 0

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        self.count += 1
        log.info("LLM call #%d (%d messages in context)", self.count, len(messages[0]))


def _pending_stories(state) -> list[dict]:
    """Stories sitting in publish_story calls that are paused for approval."""
    stories = []
    for interrupt in state.interrupts:
        for action in interrupt.value.get("action_requests", []):
            if action["name"] == PUBLISH_TOOL:
                stories.append(action["args"])
    return stories


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


async def _status(thread_id: str, llm_calls: int | None = None) -> dict:
    state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
    if not state.values:
        raise HTTPException(404, f"No run found for thread_id {thread_id}")

    body: dict = {"thread_id": thread_id}
    if llm_calls is not None:
        body["llm_calls"] = llm_calls

    pending = _pending_stories(state)
    if pending:
        deadline = approval_deadlines.setdefault(
            thread_id, datetime.now(timezone.utc) + APPROVAL_TIMEOUT
        )
        expired = datetime.now(timezone.utc) > deadline
        body.update(
            status="expired" if expired else "awaiting_approval",
            story=pending[0],
            approval_expires_at=deadline.isoformat(),
        )
        return body

    approval_deadlines.pop(thread_id, None)
    if state.next:
        # The graph stopped part-way without a pause: a step raised.
        body.update(
            status="failed",
            error=run_errors.get(thread_id, "The run stopped with an error."),
            failed_at=list(state.next),
        )
        return body

    body.update(status="completed", reply=_text(state.values["messages"][-1].content))
    return body


async def _run(thread_id: str, payload) -> dict:
    counter = LLMCallCounter()
    config = {"configurable": {"thread_id": thread_id}, "callbacks": [counter]}
    run_errors.pop(thread_id, None)
    try:
        await agent.ainvoke(payload, config)
    except Exception as exc:
        state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
        if not state.values:  # failed before anything was saved, e.g. bad API key
            log.exception("Agent run failed")
            raise HTTPException(502, f"Agent run failed: {exc}") from exc
        run_errors[thread_id] = f"{type(exc).__name__.lstrip('_')}: {exc}"
        log.error("Run %s failed: %s", thread_id, run_errors[thread_id])
    return await _status(thread_id, counter.count)


@app.post("/chat")
async def chat(req: ChatRequest):
    thread_id = req.thread_id or str(uuid.uuid4())
    state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
    if state.interrupts:
        raise HTTPException(
            409, "This thread has a story awaiting approval; decide on it first."
        )
    if state.next:
        raise HTTPException(409, "This thread's run failed; start a new thread.")
    return await _run(thread_id, {"messages": [{"role": "user", "content": req.message}]})


@app.post("/chat/{thread_id}/decision")
async def decide(thread_id: str, req: DecisionRequest):
    current = await _status(thread_id)
    if current["status"] == "expired":
        raise HTTPException(410, "The approval window for this story has expired.")
    if current["status"] != "awaiting_approval":
        raise HTTPException(409, "Nothing is awaiting approval on this thread.")

    if req.decision == "approve":
        decision = {"type": "approve"}
    else:
        decision = {"type": "reject", "message": req.feedback or "Rejected by reviewer."}

    # One decision per paused tool call (normally exactly one).
    state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
    count = sum(len(i.value["action_requests"]) for i in state.interrupts)
    log.info("Human decision on %s: %s", thread_id, req.decision)

    # A rejection sends the agent back to revise, which opens a new window.
    approval_deadlines.pop(thread_id, None)
    return await _run(thread_id, Command(resume={"decisions": [decision] * count}))


@app.get("/chat/{thread_id}")
async def status(thread_id: str):
    return await _status(thread_id)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(Path(__file__).parent / "index.html")

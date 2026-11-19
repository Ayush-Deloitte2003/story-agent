"""Separate read-only support service so a slow model call never sits in the booking API's request path.

It reads a minimal, passenger-free fact set, makes one non-retried advisor call, validates the
result and falls back to a deterministic answer. Questions and answers are neither stored nor logged.
"""

import json
import logging
import os
import urllib.request
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.agent_contracts import validate_support_advice
from app.store import PostgresStore
from app.support import allowed_actions, deterministic_answer, refund_preview, scrub_question

log = logging.getLogger("step4.support")
store = PostgresStore()
counters = {"requests": 0, "advisor": 0, "fallback": 0, "not_found": 0}
app = FastAPI(title="Flight support service (read-only, simulated)")


class SupportQuestion(BaseModel):
    booking_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=500)


def _call_advisor(payload: dict) -> dict:
    url = os.getenv("SUPPORT_ADVISOR_URL", "http://127.0.0.1:8105/answer")
    timeout = float(os.getenv("SUPPORT_ADVISOR_TIMEOUT_SECONDS", "5"))
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


@app.get("/healthz")
def healthz():
    store.ping()
    return {"status": "ok"}


@app.get("/support/status")
def status():
    return {"counters": dict(counters)}


@app.post("/support/ask")
def ask(body: SupportQuestion):
    request_id = uuid.uuid4().hex[:12]
    counters["requests"] += 1
    facts = store.support_facts(body.booking_id)
    if facts is None:
        counters["not_found"] += 1
        log.info("support request=%s outcome=not_found", request_id)
        raise HTTPException(404, "Booking not found")
    question = scrub_question(body.question)
    actions = allowed_actions(facts)
    preview = refund_preview(facts)
    source, advice = "advisor", None
    try:
        advice = validate_support_advice(
            _call_advisor({"facts": facts, "question": question, "allowed_actions": actions,
                           "refund_preview": preview}),
            actions)
    except Exception as exc:
        source = "fallback"
        advice = deterministic_answer(facts, question, preview, actions)
        reason = type(exc).__name__
    counters["advisor" if source == "advisor" else "fallback"] += 1
    log.info("support request=%s booking=%s source=%s action=%s%s", request_id, body.booking_id, source,
             advice["suggested_action"], f" reason={reason}" if source == "fallback" else "")
    return {
        "booking_id": body.booking_id, "answer_draft": advice["answer"],
        "suggested_action": advice["suggested_action"],
        "requires_human_approval": advice["suggested_action"] != "none",
        "source": source, "authority": "advisory_only",
    }

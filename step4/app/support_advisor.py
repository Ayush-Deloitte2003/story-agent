"""Read-only support advisor. It drafts answers only; it cannot approve, cancel, refund or change data."""

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.agent_contracts import validate_support_advice
from app.llm import agent_mode, complete_json
from app.support import deterministic_answer

app = FastAPI(title="Support advisor sidecar")


class SupportRequest(BaseModel):
    facts: dict
    question: str = Field(max_length=500)
    allowed_actions: list[str] = Field(max_length=5)
    refund_preview: dict | None = None


def advise_support(facts: dict, question: str, allowed_actions: list[str], preview: dict | None) -> dict:
    if agent_mode() == "mock":
        return validate_support_advice(
            deterministic_answer(facts, question, preview, allowed_actions), allowed_actions)
    result = complete_json(
        "You are a read-only customer-support assistant for a simulated airline. Answer the "
        "customer's question using only the supplied facts and refund_preview. Return exactly "
        "JSON keys answer (at most 600 characters) and suggested_action, one of the supplied "
        "allowed_actions. You cannot approve, cancel, refund or change anything; any action needs "
        "human approval. Do not ask for or repeat personal data.",
        {"facts": facts, "question": question, "allowed_actions": allowed_actions,
         "refund_preview": preview},
    )
    if set(result) != {"answer", "suggested_action"}:
        raise ValueError("Support advisor returned unexpected fields")
    return validate_support_advice({**result, "authority": "advisory_only"}, allowed_actions)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/answer")
def answer(request: SupportRequest):
    return advise_support(request.facts, request.question, request.allowed_actions, request.refund_preview)

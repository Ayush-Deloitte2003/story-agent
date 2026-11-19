"""Advisory cancellation-policy agent. It cannot refund, cancel, release seats or approve anything."""

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.agent_contracts import validate_cancellation_advice
from app.inventory import refund_for
from app.llm import agent_mode, complete_json

app = FastAPI(title="Cancellation policy advisor sidecar")


class CancellationAdviceRequest(BaseModel):
    flight_id: str = Field(min_length=1, max_length=40)
    paid_cents: int = Field(ge=0, le=500_000)
    depart_at: str = Field(min_length=1, max_length=40)
    requested_at: str = Field(min_length=1, max_length=40)


def advise_cancellation(flight_id: str, paid_cents: int, depart_at: str, requested_at: str) -> dict:
    fare = refund_for(paid_cents, depart_at, requested_at)
    if agent_mode() == "mock":
        return {
            "eligibility": fare["eligibility"],
            "proposed_refund_cents": fare["refund_cents"],
            "fare_rule": fare["fare_rule"],
            "rationale": "Deterministic mock policy advice; nothing was refunded or cancelled.",
            "authority": "advisory_only",
        }
    result = complete_json(
        "You are an advisory cancellation-policy analyst for a simulated airline. Fare rules: "
        "full_refund_72h (100%) at 72h or more before departure, half_refund_24h (50%) from 24h "
        "up to 72h, no_refund_under_24h (0%) below 24h. Return exactly JSON keys eligibility "
        "(eligible, partial or ineligible), proposed_refund_cents (integer), fare_rule and "
        "rationale (at most 180 characters). Never refund, cancel or approve; a human decides.",
        {"flight_id": flight_id, "paid_cents": paid_cents, "depart_at": depart_at,
         "requested_at": requested_at},
    )
    if set(result) != {"eligibility", "proposed_refund_cents", "fare_rule", "rationale"}:
        raise ValueError("Cancellation advisor returned unexpected fields")
    return validate_cancellation_advice({**result, "authority": "advisory_only"}, fare)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/advise")
def advise(request: CancellationAdviceRequest):
    return advise_cancellation(request.flight_id, request.paid_cents, request.depart_at,
                               request.requested_at)

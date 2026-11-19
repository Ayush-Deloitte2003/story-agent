"""Advisory payment-risk agent. It has no gateway or payment-write capability."""

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.agent_contracts import validate_payment_risk_advice
from app.llm import agent_mode, complete_json

app = FastAPI(title="Simulated payment risk advisor")


class PaymentRiskRequest(BaseModel):
    flight_id: str
    seat: str
    amount_cents: int = Field(gt=0, le=500_000)


def assess_payment_risk(flight_id: str, seat: str, amount_cents: int) -> dict:
    if not flight_id or not seat or not 1 <= amount_cents <= 500_000:
        raise ValueError("Payment-risk input is invalid")
    if agent_mode() == "mock":
        return {
            "risk_level": "low" if amount_cents <= 25_000 else "review",
            "rationale": "Deterministic mock risk assessment; no payment provider was contacted.",
            "authority": "advisory_only",
        }
    result = complete_json(
        "You are an advisory risk analyst for a simulated payment only. "
        "Return exactly JSON keys risk_level and rationale. risk_level must be low, "
        "review, or high. Never request card or personal data and never charge, refund, "
        "or contact a payment provider. A human approval remains mandatory.",
        {"flight_id": flight_id, "seat": seat, "amount_cents": amount_cents},
    )
    if set(result) != {"risk_level", "rationale"}:
        raise ValueError("Payment advisor returned unexpected fields")
    risk_level = result["risk_level"]
    rationale = result["rationale"]
    if risk_level not in ("low", "review", "high"):
        raise ValueError("Payment advisor returned an invalid risk level")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 180:
        raise ValueError("Payment advisor rationale must be 1-180 characters")
    advice = {
        "risk_level": risk_level,
        "rationale": rationale,
        "authority": "advisory_only",
    }
    return validate_payment_risk_advice(advice)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/assess")
def assess(request: PaymentRiskRequest):
    return assess_payment_risk(request.flight_id, request.seat, request.amount_cents)

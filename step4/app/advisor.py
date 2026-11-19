"""Flight disruption advisor sidecar; recommendations never change bookings."""

from app.llm import agent_mode, complete_json
from fastapi import FastAPI
from pydantic import BaseModel

from app.agent_contracts import ADVISORY_AUTHORITY
from app.inventory import recommend_alternatives

app = FastAPI(title="Flight recommendation sidecar")


class RecommendationRequest(BaseModel):
    flight_id: str


def advise_rebooking(flight_id: str) -> dict:
    alternatives = recommend_alternatives(flight_id)
    if not alternatives:
        return {
            "alternatives": [],
            "recommended_flight_id": None,
            "rationale": "No same-route mock alternatives are available.",
            "authority": ADVISORY_AUTHORITY,
        }
    if agent_mode() == "mock":
        return {
            "alternatives": alternatives,
            "recommended_flight_id": alternatives[0]["flight_id"],
            "rationale": "First same-route mock alternative; human approval is still required.",
            "authority": ADVISORY_AUTHORITY,
        }

    result = complete_json(
        "You are a read-only disruption advisor. Select one flight_id from the supplied "
        "same-route mock alternatives, or null if none is suitable. Return exactly JSON "
        "keys recommended_flight_id and rationale. Never hold, book, charge, refund, "
        "or ticket a flight; a human must choose and approve.",
        {"disrupted_flight_id": flight_id, "alternatives": alternatives},
    )
    if set(result) != {"recommended_flight_id", "rationale"}:
        raise ValueError("Disruption advisor returned unexpected fields")
    recommended = result["recommended_flight_id"]
    allowed = {item["flight_id"] for item in alternatives}
    if recommended is not None and (
        not isinstance(recommended, str) or recommended not in allowed
    ):
        raise ValueError("Disruption advisor selected a flight outside the alternatives")
    rationale = result["rationale"]
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 180:
        raise ValueError("Disruption advisor rationale must be 1-180 characters")
    return {
        "alternatives": alternatives,
        "recommended_flight_id": recommended,
        "rationale": rationale,
        "authority": ADVISORY_AUTHORITY,
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/recommend")
def recommend(request: RecommendationRequest):
    return advise_rebooking(request.flight_id)

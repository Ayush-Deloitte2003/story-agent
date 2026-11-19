"""Advisory fare/itinerary comparison agent. It never selects, holds or reserves anything."""

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.agent_contracts import validate_fare_advice
from app.inventory import compare_options, deterministic_fare_advice
from app.llm import agent_mode, complete_json

app = FastAPI(title="Fare and itinerary advisor sidecar")


class FareRequest(BaseModel):
    flight_id: str = Field(min_length=1, max_length=40)


def advise_fare(flight_id: str) -> dict:
    if agent_mode() == "mock":
        return validate_fare_advice(deterministic_fare_advice(flight_id), flight_id)
    options = compare_options(flight_id)
    result = complete_json(
        "You are a read-only fare and itinerary analyst for a simulated airline. Compare the "
        "supplied options on fare, checked-bag fee, layover and connection risk. Return exactly "
        "JSON keys ranked_flight_ids (every supplied flight_id once, best first) and summary "
        "(at most 240 characters). Use only supplied data. Never select, hold, reserve, charge or "
        "ticket anything; a human decides.",
        {"requested_flight_id": flight_id, "options": options},
    )
    if set(result) != {"ranked_flight_ids", "summary"}:
        raise ValueError("Fare advisor returned unexpected fields")
    return validate_fare_advice(
        {"comparison": options, "ranked_flight_ids": result["ranked_flight_ids"],
         "summary": result["summary"], "authority": "advisory_only"},
        flight_id,
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/compare")
def compare(request: FareRequest):
    return advise_fare(request.flight_id)

"""Read-only booking/search advisor; it never queries seat availability or mutates bookings."""

from fastapi import FastAPI
from pydantic import BaseModel

from app.agent_contracts import validate_booking_advice
from app.llm import agent_mode, complete_json
from app.inventory import get_flight, recommend_alternatives

app = FastAPI(title="Booking policy advisor sidecar")


class BookingAdviceRequest(BaseModel):
    flight_id: str
    seat: str


def advise_booking(flight_id: str, seat: str) -> dict:
    flight = get_flight(flight_id)
    if not flight:
        return {
            "recommendation": "manual_review",
            "reason": "Flight is not in mock inventory.",
            "authority": "advisory_only",
        }
    alternatives = recommend_alternatives(flight_id)
    if seat not in flight["seats"]:
        return {
            "recommendation": "manual_review",
            "reason": "Seat is not in the mock seat catalog.",
            "authority": "advisory_only",
        }
    recommendation = "eligible_for_human_review"
    reason = "Flight and seat are present in mock inventory; current availability is not checked."
    alternative_ids = [item["flight_id"] for item in alternatives]
    if agent_mode() == "llm":
        result = complete_json(
            "You are a read-only flight search advisor. Use only supplied mock inventory. "
            "Return exactly JSON keys recommendation, summary, and alternative_flight_ids. "
            "recommendation must be eligible_for_human_review or manual_review. "
            "Do not claim current availability and do not hold, charge, or ticket anything.",
            {"flight_id": flight_id, "requested_seat": seat, "mock_seats": list(flight["seats"]),
             "same_route_alternatives": alternatives},
        )
        if set(result) != {"recommendation", "summary", "alternative_flight_ids"}:
            raise ValueError("Booking advisor returned unexpected fields")
        recommendation = result["recommendation"]
        reason = result["summary"]
        alternative_ids = result["alternative_flight_ids"]
        if recommendation not in ("eligible_for_human_review", "manual_review"):
            raise ValueError("Booking advisor returned an invalid recommendation")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 180:
            raise ValueError("Booking advisor summary must be 1-180 characters")
        if (
            not isinstance(alternative_ids, list)
            or len(alternative_ids) > len(alternatives)
            or any(not isinstance(item, str) for item in alternative_ids)
            or len(set(alternative_ids)) != len(alternative_ids)
            or any(item not in {flight["flight_id"] for flight in alternatives} for item in alternative_ids)
        ):
            raise ValueError("Booking advisor recommended an unknown alternative")
    alternative_by_id = {item["flight_id"]: item for item in alternatives}
    advice = {
        "recommendation": recommendation,
        "reason": reason,
        "flight_id": flight_id,
        "price_cents": flight["price_cents"],
        "mock_seat_catalog": list(flight["seats"]),
        "same_route_alternatives": [alternative_by_id[item] for item in alternative_ids],
        "authority": "advisory_only",
    }
    return validate_booking_advice(advice, flight_id, seat)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/advise")
def advise(request: BookingAdviceRequest):
    return advise_booking(request.flight_id, request.seat)

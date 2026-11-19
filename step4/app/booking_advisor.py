"""Read-only, deterministic booking catalog and policy recommendations."""

from fastapi import FastAPI
from pydantic import BaseModel

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
    if seat not in flight["seats"]:
        return {
            "recommendation": "manual_review",
            "reason": "Seat is not in the mock seat catalog.",
            "authority": "advisory_only",
        }
    return {
        "recommendation": "eligible_for_human_review",
        "reason": "Flight and seat are present in mock inventory; current availability is not checked.",
        "flight_id": flight_id,
        "price_cents": flight["price_cents"],
        "mock_seat_catalog": list(flight["seats"]),
        "same_route_alternatives": recommend_alternatives(flight_id),
        "authority": "advisory_only",
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/advise")
def advise(request: BookingAdviceRequest):
    return advise_booking(request.flight_id, request.seat)

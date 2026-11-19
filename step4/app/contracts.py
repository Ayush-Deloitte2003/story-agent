from typing import Literal

from pydantic import BaseModel, Field


class BookingRequest(BaseModel):
    flight_id: str = Field(min_length=1, max_length=40)
    seat: str = Field(min_length=1, max_length=8)
    passenger_name: str = Field(min_length=1, max_length=120)
    hold_seconds: int = Field(default=300, ge=2, le=3600)
    failure_at: Literal["payment", "ticketing"] | None = None


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    flight_id: str | None = Field(default=None, max_length=40)


class DisruptionEvent(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=512)
    booking_id: str = Field(min_length=1, max_length=100)
    flight_id: str = Field(min_length=1, max_length=40)


def booking_payload(request: BookingRequest, failure_injection_enabled: bool) -> dict:
    if request.failure_at and not failure_injection_enabled:
        raise ValueError("Failure injection is disabled")
    return request.model_dump()

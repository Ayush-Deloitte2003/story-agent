import hashlib
import json
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
    request_hash: str | None = Field(default=None, max_length=64)
    refund_cents: int | None = Field(default=None, ge=0, le=500_000)


CANCELLATION_ONLY_FIELDS = ("request_hash", "refund_cents")


class CancellationRequest(BaseModel):
    model_config = {"extra": "forbid"}
    booking_id: str = Field(min_length=1, max_length=100)
    approval_seconds: int = Field(default=300, ge=2, le=3600)
    failure_at: Literal["refund", "release"] | None = None


def cancellation_request_hash(workflow_id: str, booking_id: str, snapshot: dict, refund_cents: int,
                              fare_rule: str) -> str:
    """Binds an approval to the exact cancellation, ticket and refund amount."""
    material = [workflow_id, booking_id, snapshot["flight_id"], snapshot["seat"], refund_cents, fare_rule]
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()


def cancellation_payload(request: CancellationRequest, failure_injection_enabled: bool) -> dict:
    if request.failure_at and not failure_injection_enabled:
        raise ValueError("Failure injection is disabled")
    return request.model_dump()


class DisruptionEvent(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=512)
    booking_id: str = Field(min_length=1, max_length=100)
    flight_id: str = Field(min_length=1, max_length=40)


def booking_payload(request: BookingRequest, failure_injection_enabled: bool) -> dict:
    if request.failure_at and not failure_injection_enabled:
        raise ValueError("Failure injection is disabled")
    return request.model_dump()

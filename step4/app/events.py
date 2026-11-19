import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, Field, ValidationError

BOOKING_REQUESTED = "com.example.booking.requested"
FLIGHT_DISRUPTED = "com.example.flight.disrupted"


class DisruptionData(BaseModel):
    model_config = {"extra": "forbid"}
    booking_id: str = Field(min_length=1, max_length=100)
    flight_id: str = Field(min_length=1, max_length=40)


class BookingDispatchData(BaseModel):
    workflow_id: str = Field(min_length=1, max_length=100)
    booking_id: str = Field(min_length=1, max_length=100)
    request: dict


@dataclass(frozen=True)
class EventType:
    name: str
    model: type[BaseModel]
    app_id: str
    method: str
    workflow_id: Callable[[str, str, dict], str]
    external: bool


def _disruption_workflow_id(source: str, event_id: str, _data: dict) -> str:
    return "disruption-" + hashlib.sha256(f"{source}:{event_id}".encode()).hexdigest()[:40]


REGISTRY: dict[str, EventType] = {
    FLIGHT_DISRUPTED: EventType(
        FLIGHT_DISRUPTED, DisruptionData, "disruption-worker",
        "internal/disruptions/schedule", _disruption_workflow_id, external=True,
    ),
    BOOKING_REQUESTED: EventType(
        BOOKING_REQUESTED, BookingDispatchData, "booking-worker",
        "internal/bookings/schedule", lambda _s, _e, data: data["workflow_id"], external=False,
    ),
}


@dataclass(frozen=True)
class Accepted:
    event_type: EventType
    source: str
    event_id: str
    data: dict
    workflow_id: str
    payload_hash: str


@dataclass(frozen=True)
class Quarantined:
    reason: str
    event_type: str | None
    source: str | None
    event_id: str | None
    quarantine_id: str


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _quarantine(raw, reason, event_type=None, source=None, event_id=None) -> Quarantined:
    return Quarantined(reason, event_type, source, event_id, canonical_hash([reason, raw]))


def classify(raw: dict) -> Accepted | Quarantined:
    """Validate an inbound event against the registry; never raises for bad content."""
    if not isinstance(raw, dict):
        return _quarantine(raw, "Event must be a JSON object")
    event_id, source = raw.get("id"), raw.get("source")
    event_type = raw.get("type", FLIGHT_DISRUPTED)
    ident = (event_type if isinstance(event_type, str) else None,
             source if isinstance(source, str) else None,
             event_id if isinstance(event_id, str) else None)
    if not all(isinstance(v, str) and v for v in (event_id, source, event_type)):
        return _quarantine(raw, "Event requires non-empty string id, source and type", *ident)
    if len(event_id) > 256 or len(source) > 512:
        return _quarantine(raw, "Event id or source is too long", *ident)
    spec = REGISTRY.get(event_type)
    if spec is None or not spec.external:
        return _quarantine(raw, "Unknown event type", *ident)
    data = raw["data"] if isinstance(raw.get("data"), dict) else {
        key: raw[key] for key in ("booking_id", "flight_id") if key in raw
    }
    try:
        data = spec.model.model_validate(data).model_dump()
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
        return _quarantine(raw, f"Invalid event data: {', '.join(fields)}", *ident)
    return Accepted(spec, source, event_id, data, spec.workflow_id(source, event_id, data),
                    canonical_hash([event_type, data]))

"""Mock airline inventory; this module never calls an airline or reserves a real seat."""

from datetime import datetime

FLIGHTS = {
    "SA101": {"origin": "SFO", "destination": "SEA", "depart_at": "2027-06-01T09:00:00Z", "seats": ("1A", "1B", "2A"), "price_cents": 18000},
    "SA102": {"origin": "SFO", "destination": "SEA", "depart_at": "2027-06-01T13:00:00Z", "seats": ("1A", "1B", "2A"), "price_cents": 21000},
    "SA201": {"origin": "SFO", "destination": "LAX", "depart_at": "2027-06-01T11:00:00Z", "seats": ("1A", "1B", "2A"), "price_cents": 14500},
}


def get_flight(flight_id: str) -> dict | None:
    return FLIGHTS.get(flight_id)


def refund_for(paid_cents: int, depart_at: str, requested_at: str) -> dict:
    """Deterministic fare rule over values snapshotted when the cancellation was accepted.

    Later catalog or schedule changes must not alter a computed amount, so callers pass
    the snapshot rather than reading the live catalog.
    """
    if isinstance(paid_cents, bool) or not isinstance(paid_cents, int) or paid_cents < 0:
        raise ValueError("Paid amount must be a non-negative integer")
    departs = datetime.fromisoformat(depart_at.replace("Z", "+00:00"))
    requested = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
    if departs.tzinfo is None or requested.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware")
    hours = (departs - requested).total_seconds() / 3600
    if hours >= 72:
        rule, percent, eligibility = "full_refund_72h", 100, "eligible"
    elif hours >= 24:
        rule, percent, eligibility = "half_refund_24h", 50, "partial"
    else:
        rule, percent, eligibility = "no_refund_under_24h", 0, "ineligible"
    return {"fare_rule": rule, "eligibility": eligibility, "refund_percent": percent,
            "refund_cents": paid_cents * percent // 100}


def recommend_alternatives(flight_id: str) -> list[dict]:
    disrupted = get_flight(flight_id)
    if disrupted is None:
        return []
    return [
        {"flight_id": candidate_id, "depart_at": candidate["depart_at"], "price_cents": candidate["price_cents"]}
        for candidate_id, candidate in FLIGHTS.items()
        if candidate_id != flight_id
        and candidate["origin"] == disrupted["origin"]
        and candidate["destination"] == disrupted["destination"]
    ]

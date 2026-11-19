"""Mock airline inventory; this module never calls an airline or reserves a real seat."""

from datetime import datetime

FLIGHTS = {
    "SA101": {"origin": "SFO", "destination": "SEA", "depart_at": "2027-06-01T09:00:00Z", "seats": ("1A", "1B", "2A"), "price_cents": 18000},
    "SA102": {"origin": "SFO", "destination": "SEA", "depart_at": "2027-06-01T13:00:00Z", "seats": ("1A", "1B", "2A"), "price_cents": 21000},
    "SA201": {"origin": "SFO", "destination": "LAX", "depart_at": "2027-06-01T11:00:00Z", "seats": ("1A", "1B", "2A"), "price_cents": 14500},
}


# Richer mock itinerary data, kept apart from FLIGHTS so seeding, alternatives and existing
# contracts are unchanged. It is read-only reference data for the fare/itinerary advisor.
ITINERARY_DETAILS = {
    "SA101": {"fare_class": "basic", "arrive_at": "2027-06-01T11:10:00Z", "layover_minutes": 0,
              "min_connection_minutes": 0, "on_time_percent": 88,
              "carry_on_kg": 7, "checked_kg": 0, "checked_fee_cents": 3500},
    "SA102": {"fare_class": "flex", "arrive_at": "2027-06-01T16:05:00Z", "layover_minutes": 55,
              "min_connection_minutes": 45, "on_time_percent": 76,
              "carry_on_kg": 10, "checked_kg": 23, "checked_fee_cents": 0},
    "SA201": {"fare_class": "basic", "arrive_at": "2027-06-01T12:25:00Z", "layover_minutes": 0,
              "min_connection_minutes": 0, "on_time_percent": 91,
              "carry_on_kg": 7, "checked_kg": 0, "checked_fee_cents": 3000},
}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def get_flight(flight_id: str) -> dict | None:
    return FLIGHTS.get(flight_id)


def connection_risk(details: dict) -> str:
    if details["layover_minutes"] == 0:
        return "low"
    slack = details["layover_minutes"] - details["min_connection_minutes"]
    if slack < 0:
        return "high"
    return "medium" if slack < 30 else "low"


def compare_options(flight_id: str) -> list[dict]:
    """The requested flight first, then same-route alternatives, each from the mock catalog."""
    flight = get_flight(flight_id)
    if flight is None:
        return []
    ids = [flight_id] + [item["flight_id"] for item in recommend_alternatives(flight_id)]
    options = []
    for candidate_id in ids:
        candidate, details = FLIGHTS[candidate_id], ITINERARY_DETAILS[candidate_id]
        options.append({
            "flight_id": candidate_id, "price_cents": candidate["price_cents"],
            "fare_class": details["fare_class"], "depart_at": candidate["depart_at"],
            "arrive_at": details["arrive_at"], "layover_minutes": details["layover_minutes"],
            "carry_on_kg": details["carry_on_kg"], "checked_kg": details["checked_kg"],
            "checked_fee_cents": details["checked_fee_cents"],
            "connection_risk": connection_risk(details),
        })
    return options


def deterministic_fare_advice(flight_id: str) -> dict:
    """Catalog-only comparison used by the mock advisor and as the workflow fallback."""
    options = compare_options(flight_id)
    ranked = sorted(options, key=lambda o: (RISK_ORDER[o["connection_risk"]],
                                            o["price_cents"] + o["checked_fee_cents"], o["flight_id"]))
    summary = ("Flight is not in mock inventory." if not options else
               f"Lowest connection risk and total fare with a checked bag: {ranked[0]['flight_id']}. "
               "Comparison only; nothing is selected.")
    return {"comparison": options, "ranked_flight_ids": [o["flight_id"] for o in ranked],
            "summary": summary, "authority": "advisory_only"}


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

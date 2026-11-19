"""Mock airline inventory; this module never calls an airline or reserves a real seat."""

FLIGHTS = {
    "SA101": {"origin": "SFO", "destination": "SEA", "depart_at": "2027-06-01T09:00:00Z", "seats": ("1A", "1B", "2A"), "price_cents": 18000},
    "SA102": {"origin": "SFO", "destination": "SEA", "depart_at": "2027-06-01T13:00:00Z", "seats": ("1A", "1B", "2A"), "price_cents": 21000},
    "SA201": {"origin": "SFO", "destination": "LAX", "depart_at": "2027-06-01T11:00:00Z", "seats": ("1A", "1B", "2A"), "price_cents": 14500},
}


def get_flight(flight_id: str) -> dict | None:
    return FLIGHTS.get(flight_id)


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

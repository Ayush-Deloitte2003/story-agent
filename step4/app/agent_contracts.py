"""Strict workflow-side validation for untrusted agent sidecar output."""

from app.inventory import get_flight, recommend_alternatives

ADVISORY_AUTHORITY = "advisory_only"


def validate_booking_advice(result: dict, flight_id: str, seat: str) -> dict:
    if not isinstance(result, dict):
        raise ValueError("Booking advisor result must be an object")
    required = {"recommendation", "reason", "authority"}
    allowed = required | {
        "flight_id", "price_cents", "mock_seat_catalog", "same_route_alternatives",
    }
    if not required.issubset(result) or not set(result).issubset(allowed):
        raise ValueError("Booking advisor result has an invalid shape")
    if result["authority"] != ADVISORY_AUTHORITY:
        raise ValueError("Booking advisor must remain advisory")
    if result["recommendation"] not in ("eligible_for_human_review", "manual_review"):
        raise ValueError("Booking advisor recommendation is invalid")
    reason = result["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 180:
        raise ValueError("Booking advisor reason must be 1-180 characters")
    flight = get_flight(flight_id)
    if not flight:
        if set(result) != required:
            raise ValueError("Unknown flights cannot have catalog details")
        if result["recommendation"] != "manual_review":
            raise ValueError("Unknown flights require manual review")
        return result
    if seat not in flight["seats"]:
        if set(result) != required:
            raise ValueError("Unknown seats cannot have catalog details")
        if result["recommendation"] != "manual_review":
            raise ValueError("Unknown seats require manual review")
        return result
    if (
        result.get("flight_id") != flight_id
        or result.get("price_cents") != flight["price_cents"]
        or result.get("mock_seat_catalog") != list(flight["seats"])
    ):
        raise ValueError("Booking advisor changed authoritative mock catalog data")
    alternatives = result.get("same_route_alternatives")
    expected = recommend_alternatives(flight_id)
    expected_by_id = {item["flight_id"]: item for item in expected}
    if (
        not isinstance(alternatives, list)
        or len(alternatives) > len(expected)
        or any(not isinstance(item, dict) for item in alternatives)
        or any(item not in expected for item in alternatives)
        or len({item["flight_id"] for item in alternatives}) != len(alternatives)
    ):
        raise ValueError("Booking advisor returned an invalid alternative")
    if any(item["flight_id"] not in expected_by_id for item in alternatives):
        raise ValueError("Booking advisor returned an unknown alternative")
    return result


def validate_payment_risk_advice(result: dict) -> dict:
    if not isinstance(result, dict) or set(result) != {"risk_level", "rationale", "authority"}:
        raise ValueError("Payment advisor result has an invalid shape")
    if result["authority"] != ADVISORY_AUTHORITY:
        raise ValueError("Payment advisor must remain advisory")
    if result["risk_level"] not in ("low", "review", "high"):
        raise ValueError("Payment advisor risk level is invalid")
    rationale = result["rationale"]
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 180:
        raise ValueError("Payment advisor rationale must be 1-180 characters")
    return result


def validate_disruption_advice(result: dict, booking_id: str, flight_id: str) -> dict:
    expected_alternatives = recommend_alternatives(flight_id)
    if not isinstance(result, dict) or set(result) != {
        "booking_id", "flight_id", "alternatives", "recommended_flight_id", "rationale", "authority",
    }:
        raise ValueError("Disruption advisor result has an invalid shape")
    if result["booking_id"] != booking_id or result["flight_id"] != flight_id:
        raise ValueError("Disruption advisor changed workflow identity")
    if result["authority"] != ADVISORY_AUTHORITY:
        raise ValueError("Disruption advisor must remain advisory")
    if result["alternatives"] != expected_alternatives:
        raise ValueError("Disruption advisor changed the available alternatives")
    recommended = result["recommended_flight_id"]
    if recommended is not None and (
        not isinstance(recommended, str)
        or recommended not in {item["flight_id"] for item in expected_alternatives}
    ):
        raise ValueError("Disruption advisor recommended an unlisted flight")
    rationale = result["rationale"]
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 180:
        raise ValueError("Disruption advisor rationale must be 1-180 characters")
    return result

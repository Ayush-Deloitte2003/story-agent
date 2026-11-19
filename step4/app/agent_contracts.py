"""Strict workflow-side validation for untrusted agent sidecar output."""

from app.inventory import compare_options, get_flight, recommend_alternatives

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


def validate_fare_advice(result: dict, flight_id: str) -> dict:
    """Comparison data must equal the mock catalog; the ranking may only reorder known flights."""
    expected = compare_options(flight_id)
    keys = {"comparison", "ranked_flight_ids", "summary", "authority"}
    if not isinstance(result, dict) or set(result) != keys:
        raise ValueError("Fare advisor result has an invalid shape")
    if result["authority"] != ADVISORY_AUTHORITY:
        raise ValueError("Fare advisor must remain advisory")
    if result["comparison"] != expected:
        raise ValueError("Fare advisor changed the catalog comparison")
    ranked = result["ranked_flight_ids"]
    if (not isinstance(ranked, list) or any(not isinstance(item, str) for item in ranked)
            or sorted(ranked) != sorted(option["flight_id"] for option in expected)):
        raise ValueError("Fare advisor ranking must list each known option exactly once")
    summary = result["summary"]
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 240:
        raise ValueError("Fare advisor summary must be 1-240 characters")
    return result


SUPPORT_ACTIONS = ("none", "request_cancellation", "contact_agent")


def validate_support_advice(result: dict, allowed_actions: list[str]) -> dict:
    if not isinstance(result, dict) or set(result) != {"answer", "suggested_action", "authority"}:
        raise ValueError("Support advisor result has an invalid shape")
    if result["authority"] != ADVISORY_AUTHORITY:
        raise ValueError("Support advisor must remain advisory")
    if result["suggested_action"] not in SUPPORT_ACTIONS or result["suggested_action"] not in allowed_actions:
        raise ValueError("Support advisor suggested an action that is not allowed")
    answer = result["answer"]
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 600:
        raise ValueError("Support advisor answer must be 1-600 characters")
    return result


def validate_cancellation_advice(result: dict, fare: dict) -> dict:
    """The advisor may only restate the deterministic fare result; any deviation is rejected."""
    keys = {"eligibility", "proposed_refund_cents", "fare_rule", "rationale", "authority"}
    if not isinstance(result, dict) or set(result) != keys:
        raise ValueError("Cancellation advisor result has an invalid shape")
    if result["authority"] != ADVISORY_AUTHORITY:
        raise ValueError("Cancellation advisor must remain advisory")
    proposed = result["proposed_refund_cents"]
    if isinstance(proposed, bool) or not isinstance(proposed, int):
        raise ValueError("Cancellation advisor refund must be an integer")
    if result["eligibility"] != fare["eligibility"]:
        raise ValueError("Cancellation advisor eligibility differs from the fare rule")
    if result["fare_rule"] != fare["fare_rule"]:
        raise ValueError("Cancellation advisor applied a different fare rule")
    if proposed != fare["refund_cents"]:
        raise ValueError("Cancellation advisor refund differs from the fare rule")
    rationale = result["rationale"]
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 180:
        raise ValueError("Cancellation advisor rationale must be 1-180 characters")
    return result

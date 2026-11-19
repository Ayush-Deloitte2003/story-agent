"""Pure helpers for the read-only support path: question scrubbing, allowed actions and fallback text."""

import re
from datetime import datetime, timezone

from app.inventory import refund_for

MAX_QUESTION_CHARS = 500
ACTIVE_CANCELLATION = ("scheduled", "awaiting_approval")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_NUMBER_RUN = re.compile(r"\d[\d \-]{4,}\d")


def scrub_question(question: str) -> str:
    """Best-effort removal of obvious emails and long digit runs; it does not anonymize free text."""
    text = question.strip()[:MAX_QUESTION_CHARS]
    text = _EMAIL.sub("[email]", text)
    return _NUMBER_RUN.sub("[number]", text)


def allowed_actions(facts: dict) -> list[str]:
    actions = ["none", "contact_agent"]
    if facts["booking_status"] == "confirmed" and facts.get("cancellation_status") not in ACTIVE_CANCELLATION:
        actions.insert(1, "request_cancellation")
    return actions


def refund_preview(facts: dict, now: datetime | None = None) -> dict | None:
    """What the deterministic fare rule would refund if a cancellation were requested now."""
    if facts["booking_status"] != "confirmed" or facts.get("paid_cents") is None:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        return refund_for(facts["paid_cents"], facts["depart_at"], now.isoformat())
    except ValueError:
        return None


def deterministic_answer(facts: dict, question: str, preview: dict | None, actions: list[str]) -> dict:
    lowered = question.lower()
    parts = [f"Your booking is {facts['booking_status'].replace('_', ' ')}."]
    if facts.get("cancellation_status"):
        parts.append(f"The latest cancellation request is {facts['cancellation_status'].replace('_', ' ')}.")
    if facts.get("refund_status") == "issued":
        parts.append(f"A simulated refund of {facts['refund_cents']} cents was recorded.")
    wants_cancel = any(word in lowered for word in ("cancel", "refund"))
    action = "none"
    if wants_cancel and preview:
        parts.append(
            f"If cancelled now the fare rule {preview['fare_rule']} would refund "
            f"{preview['refund_cents']} cents; a human must approve any cancellation.")
        if "request_cancellation" in actions:
            action = "request_cancellation"
    elif wants_cancel:
        action = "contact_agent"
        parts.append("A cancellation cannot be requested for this booking right now.")
    return {"answer": " ".join(parts)[:600], "suggested_action": action, "authority": "advisory_only"}

"""Validation and idempotency helpers for Dapr-published story events."""

import math
import uuid


_EVENT_NAMESPACE = uuid.UUID("da982b0b-f37f-4fdb-bf63-48970dbffac1")


def parse_story_requested_event(event: object) -> tuple[str, str, dict]:
    """Return source, event ID, and workflow input for a story.requested CloudEvent."""
    if not isinstance(event, dict):
        raise ValueError("CloudEvent must be a JSON object.")

    if event.get("specversion") != "1.0":
        raise ValueError("CloudEvent specversion must be 1.0.")
    if event.get("type") != "story.requested":
        raise ValueError("CloudEvent type must be story.requested.")

    source = event.get("source")
    event_id = event.get("id")
    if not isinstance(source, str) or not source.strip() or len(source) > 512:
        raise ValueError("CloudEvent source must be a non-empty string.")
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 256:
        raise ValueError("CloudEvent id must be a non-empty string.")

    data = event.get("data")
    if not isinstance(data, dict):
        raise ValueError("CloudEvent data must be a JSON object.")
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Story request message must be a non-empty string.")

    workflow_input = {"message": message}
    timeout = data.get("approval_timeout_seconds")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("approval_timeout_seconds must be a positive number.")
        try:
            timeout = float(timeout)
        except (OverflowError, ValueError) as exc:
            raise ValueError("approval_timeout_seconds must be a positive finite number.") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("approval_timeout_seconds must be a positive finite number.")
        workflow_input["approval_timeout_seconds"] = timeout

    return source, event_id, workflow_input


def workflow_id_for_event(source: str, event_id: str) -> str:
    """Map a CloudEvent identity to a stable, Dapr-safe workflow instance ID."""
    if (not isinstance(source, str) or not source.strip()
            or len(source) > 512
            or not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 256):
        raise ValueError("CloudEvent source and id must be non-empty.")
    identity = f"{source}\0{event_id}"
    return f"story-event-{uuid.uuid5(_EVENT_NAMESPACE, identity)}"

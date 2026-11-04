"""Validation and idempotency helpers for Dapr-published story events."""

import math
import uuid


_EVENT_NAMESPACE = uuid.UUID("da982b0b-f37f-4fdb-bf63-48970dbffac1")


def parse_story_requested_event(event: object) -> tuple[str, str, dict]:
    """Return source, event ID, and workflow input for a story.requested CloudEvent."""
    source, event_id, data = _event_identity_and_data(event, "story.requested")
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


def parse_story_quality_review_event(event: object) -> tuple[str, str, dict]:
    """Return source, event ID, and workflow input for a quality-review CloudEvent."""
    source, event_id, data = _event_identity_and_data(event, "story.quality-review.requested")

    title = data.get("title")
    story = data.get("story")
    request = data.get("request", "")
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise ValueError("Story title must be a non-empty string of at most 500 characters.")
    if not isinstance(story, str) or not story.strip() or len(story) > 20000:
        raise ValueError("Story must be a non-empty string of at most 20000 characters.")
    if not isinstance(request, str) or len(request) > 2000:
        raise ValueError("Story request must be a string of at most 2000 characters.")

    return source, event_id, {"title": title, "story": story, "request": request}


def _event_identity_and_data(event: object, event_type: str) -> tuple[str, str, dict]:
    if not isinstance(event, dict):
        raise ValueError("CloudEvent must be a JSON object.")
    if event.get("specversion") != "1.0":
        raise ValueError("CloudEvent specversion must be 1.0.")
    if event.get("type") != event_type:
        raise ValueError(f"CloudEvent type must be {event_type}.")

    source = event.get("source")
    event_id = event.get("id")
    if not isinstance(source, str) or not source.strip() or len(source) > 512:
        raise ValueError("CloudEvent source must be a non-empty string.")
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 256:
        raise ValueError("CloudEvent id must be a non-empty string.")
    data = event.get("data")
    if not isinstance(data, dict):
        raise ValueError("CloudEvent data must be a JSON object.")
    return source, event_id, data


def workflow_id_for_event(source: str, event_id: str) -> str:
    """Map a CloudEvent identity to a stable, Dapr-safe workflow instance ID."""
    if (not isinstance(source, str) or not source.strip()
            or len(source) > 512
            or not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 256):
        raise ValueError("CloudEvent source and id must be non-empty.")
    identity = f"{source}\0{event_id}"
    return f"story-event-{uuid.uuid5(_EVENT_NAMESPACE, identity)}"


def workflow_id_for_quality_review_event(source: str, event_id: str) -> str:
    """Map a quality-review CloudEvent to its own stable Dapr workflow ID."""
    if (not isinstance(source, str) or not source.strip()
            or len(source) > 512
            or not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 256):
        raise ValueError("CloudEvent source and id must be non-empty.")
    identity = f"story.quality-review.requested\0{source}\0{event_id}"
    return f"story-quality-review-{uuid.uuid5(_EVENT_NAMESPACE, identity)}"

import json
import math
import unittest

from app.events import (
    parse_story_quality_review_event,
    parse_story_requested_event,
    workflow_id_for_event,
    workflow_id_for_quality_review_event,
)
from app.quality_review import parse_quality_review_response


def story_event(**overrides):
    event = {
        "specversion": "1.0",
        "id": "request-123",
        "source": "booking-service",
        "type": "story.requested",
        "data": {"message": "Write a short story."},
    }
    event.update(overrides)
    return event


class EventTests(unittest.TestCase):
    def test_valid_event_becomes_story_workflow_input(self):
        source, event_id, workflow_input = parse_story_requested_event(story_event())

        self.assertEqual(source, "booking-service")
        self.assertEqual(event_id, "request-123")
        self.assertEqual(workflow_input, {"message": "Write a short story."})

    def test_positive_timeout_is_preserved(self):
        event = story_event(data={"message": "Write a story.", "approval_timeout_seconds": 30})

        _, _, workflow_input = parse_story_requested_event(event)

        self.assertEqual(workflow_input["approval_timeout_seconds"], 30.0)

    def test_workflow_id_is_stable_and_scoped_by_source(self):
        first = workflow_id_for_event("booking-service", "request-123")

        self.assertEqual(first, workflow_id_for_event("booking-service", "request-123"))
        self.assertNotEqual(first, workflow_id_for_event("other-service", "request-123"))
        self.assertNotEqual(first, workflow_id_for_event("booking-service", "request-456"))

    def test_invalid_events_are_rejected(self):
        invalid_events = [
            None,
            story_event(specversion="0.3"),
            story_event(type="invoice.created"),
            story_event(source=" "),
            story_event(id=""),
            story_event(data=[]),
            story_event(data={"message": "  "}),
            story_event(data={"message": "Story", "approval_timeout_seconds": 0}),
            story_event(data={"message": "Story", "approval_timeout_seconds": True}),
            story_event(data={"message": "Story", "approval_timeout_seconds": math.inf}),
        ]

        for event in invalid_events:
            with self.subTest(event=event), self.assertRaises(ValueError):
                parse_story_requested_event(event)

    def test_invalid_identity_is_rejected_for_workflow_id(self):
        for source, event_id in (
            ("", "id"),
            ("source", " "),
            (None, "id"),
            ("s" * 513, "id"),
            ("source", "i" * 257),
        ):
            with self.subTest(source=source, event_id=event_id), self.assertRaises(ValueError):
                workflow_id_for_event(source, event_id)

    def test_quality_review_event_becomes_isolated_review_input(self):
        event = {
            "specversion": "1.0",
            "id": "review-123",
            "source": "story-service",
            "type": "story.quality-review.requested",
            "data": {
                "title": "The Lantern",
                "story": "A student finds a lantern and helps a friend.",
                "request": "Write an uplifting story.",
            },
        }

        source, event_id, workflow_input = parse_story_quality_review_event(event)

        self.assertEqual(source, "story-service")
        self.assertEqual(event_id, "review-123")
        self.assertEqual(workflow_input, {
            "title": "The Lantern",
            "story": "A student finds a lantern and helps a friend.",
            "request": "Write an uplifting story.",
        })

    def test_quality_review_event_rejects_malformed_content(self):
        valid = {
            "specversion": "1.0",
            "id": "review-123",
            "source": "story-service",
            "type": "story.quality-review.requested",
            "data": {"title": "The Lantern", "story": "A story."},
        }
        invalid_events = [
            {**valid, "type": "story.requested"},
            {**valid, "data": {"title": "", "story": "A story."}},
            {**valid, "data": {"title": "The Lantern", "story": " "}},
            {**valid, "data": {"title": "T" * 501, "story": "A story."}},
            {**valid, "data": {"title": "The Lantern", "story": "S" * 20001}},
            {**valid, "data": {"title": "The Lantern", "story": "A story.", "request": None}},
        ]

        for event in invalid_events:
            with self.subTest(event=event), self.assertRaises(ValueError):
                parse_story_quality_review_event(event)

    def test_quality_review_workflow_id_is_stable_and_separate(self):
        review_id = workflow_id_for_quality_review_event("story-service", "event-1")

        self.assertEqual(review_id, workflow_id_for_quality_review_event("story-service", "event-1"))
        self.assertNotEqual(review_id, workflow_id_for_event("story-service", "event-1"))
        self.assertNotEqual(review_id, workflow_id_for_quality_review_event("other", "event-1"))

    def test_quality_review_result_shape_is_validated(self):
        valid = {
            "score": 4,
            "recommendation": "ready",
            "summary": "Clear and engaging.",
            "strengths": ["Warm tone"],
            "issues": [],
            "suggestions": [],
        }
        self.assertEqual(parse_quality_review_response(json.dumps(valid)), valid)

        for field, value in (
            ("score", True),
            ("score", 6),
            ("recommendation", "approved"),
            ("summary", " "),
            ("strengths", "not a list"),
            ("issues", [3]),
        ):
            invalid = {**valid, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                parse_quality_review_response(json.dumps(invalid))
        with self.assertRaises(ValueError):
            parse_quality_review_response("not json")


if __name__ == "__main__":
    unittest.main()

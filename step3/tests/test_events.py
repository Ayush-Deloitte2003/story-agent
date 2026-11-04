import math
import unittest

from app.events import parse_story_requested_event, workflow_id_for_event


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


if __name__ == "__main__":
    unittest.main()

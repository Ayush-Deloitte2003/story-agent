import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse

from app.approval import DaprDecisionClaims


class _Response:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


class DaprDecisionClaimsTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.lock = threading.Lock()

    def fake_urlopen(self, request, timeout):
        self.assertEqual(timeout, 10)
        path = urlparse(request.full_url).path
        if request.method == "POST":
            state = json.loads(request.data)[0]
            self.assertNotIn("etag", state)
            self.assertEqual(state["options"], {
                "concurrency": "first-write",
                "consistency": "strong",
            })
            with self.lock:
                if state["key"] in self.store:
                    raise HTTPError(request.full_url, 500, "etag conflict", {}, None)
                self.store[state["key"]] = state["value"]
            return _Response(204)

        key = unquote(path.rsplit("/", 1)[-1])
        with self.lock:
            value = self.store.get(key)
        if value is None:
            return _Response(204)
        return _Response(200, json.dumps(value).encode("utf-8"))

    def test_only_one_choice_wins_across_independent_claim_clients(self):
        clients = [DaprDecisionClaims() for _ in range(8)]
        decisions = ["approve", "reject"] * 32

        with patch("urllib.request.urlopen", side_effect=self.fake_urlopen):
            with ThreadPoolExecutor(max_workers=16) as pool:
                accepted = list(pool.map(
                    lambda item: clients[item[0] % len(clients)].claim(
                        "workflow-1", 0, item[1], None
                    ),
                    enumerate(decisions),
                ))

        winning_decisions = {
            decision for decision, result in zip(decisions, accepted) if result
        }
        self.assertEqual(len(winning_decisions), 1)

    def test_same_choice_retry_is_idempotent_but_changed_feedback_is_conflict(self):
        claims = DaprDecisionClaims()

        with patch("urllib.request.urlopen", side_effect=self.fake_urlopen):
            self.assertTrue(claims.claim("workflow-2", 3, "reject", "Add more detail."))
            self.assertTrue(claims.claim("workflow-2", 3, "reject", "Add more detail."))
            self.assertFalse(claims.claim("workflow-2", 3, "approve", None))
            self.assertFalse(claims.claim("workflow-2", 3, "reject", "Different feedback."))
            self.assertTrue(claims.claim("workflow-2", 4, "approve", None))


if __name__ == "__main__":
    unittest.main()

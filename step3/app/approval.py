import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request


def approval_event_name(revision: int) -> str:
    return f"approval-{revision}"


class DaprDecisionClaims:
    """Claim a workflow revision's decision atomically in the Dapr state store."""

    def __init__(self, state_store: str = "statestore") -> None:
        self.state_store = state_store
        self.dapr_url = f"http://127.0.0.1:{os.getenv('DAPR_HTTP_PORT', '3500')}"

    @staticmethod
    def _key(thread_id: str, revision: int) -> str:
        workflow_hash = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
        return f"approval-decision-{workflow_hash}-{revision}"

    @staticmethod
    def _value(decision: str, feedback: str | None) -> dict:
        feedback_hash = hashlib.sha256((feedback or "").encode("utf-8")).hexdigest()
        return {"decision": decision, "feedback_sha256": feedback_hash}

    def claim(self, thread_id: str, revision: int, decision: str, feedback: str | None) -> bool:
        key = self._key(thread_id, revision)
        value = self._value(decision, feedback)
        request = urllib.request.Request(
            f"{self.dapr_url}/v1.0/state/{self.state_store}",
            data=json.dumps([{
                "key": key,
                "value": value,
                "etag": "*",
                "options": {"concurrency": "first-write", "consistency": "strong"},
            }]).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status == 204:
                    return True
                raise RuntimeError(f"Dapr state store returned HTTP {response.status} for decision claim.")
        except urllib.error.HTTPError as exc:
            if exc.code not in (409, 500):
                raise RuntimeError(
                    f"Dapr state store returned HTTP {exc.code} for decision claim."
                ) from exc
            try:
                existing = self._get(key)
            finally:
                exc.close()
            if existing is not None:
                return existing == value
            if exc.code == 409:
                return False
            raise RuntimeError("Dapr state store failed to persist the decision claim.") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach the Dapr state store: {exc.reason}") from exc

    def _get(self, key: str) -> dict | None:
        encoded_key = urllib.parse.quote(key, safe="")
        request = urllib.request.Request(
            f"{self.dapr_url}/v1.0/state/{self.state_store}/{encoded_key}?consistency=strong",
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status == 204:
                    return None
                value = json.loads(response.read().decode("utf-8"))
                return value if isinstance(value, dict) else None
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Dapr state store returned HTTP {exc.code} while reading a decision claim."
            ) from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read the Dapr decision claim: {exc}") from exc

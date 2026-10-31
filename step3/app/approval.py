from threading import Lock


def approval_event_name(revision: int) -> str:
    return f"approval-{revision}"


class DecisionClaims:
    """Allow only one accepted decision for a workflow's current revision."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._claims: dict[str, int] = {}

    def claim(self, thread_id: str, revision: int) -> bool:
        with self._lock:
            if self._claims.get(thread_id) == revision:
                return False
            self._claims[thread_id] = revision
            return True

    def reconcile(self, thread_id: str, active_revision: int | None) -> None:
        with self._lock:
            claimed_revision = self._claims.get(thread_id)
            if claimed_revision is not None and claimed_revision != active_revision:
                del self._claims[thread_id]

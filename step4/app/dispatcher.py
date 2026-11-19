import logging
import os
import socket
import uuid

LOG = logging.getLogger("step4.dispatcher")
LEASE_SECONDS = int(os.getenv("DISPATCH_LEASE_SECONDS", "30"))
OWNER = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def backoff_seconds(attempts: int) -> int:
    return min(2 ** max(attempts - 1, 0), 30)


def dispatch_due(store, invoke, limit: int = 10, dispatch_id: str | None = None,
                 owner: str = OWNER) -> int:
    """Lease due dispatch records and hand them to workers.

    Workers schedule with stable workflow IDs, so redelivery after a lost
    acknowledgement or lease expiry is harmless. Failures are retried with
    backoff and never surfaced to the original caller.
    """
    delivered = 0
    for record in store.lease_dispatches(owner, limit, LEASE_SECONDS, dispatch_id):
        try:
            if store.workflow_started(record):
                # Never reschedule: the workflow already ran, and Dapr history may have been purged.
                if store.complete_dispatch(record["dispatch_id"], owner):
                    delivered += 1
                continue
            invoke(record["app_id"], record["method"], record["payload"])
        except Exception as exc:  # any failure, including HTTPException, is retried later
            LOG.warning("dispatch %s attempt %s failed: %s", record["dispatch_id"],
                        record["attempts"], type(exc).__name__)
            store.release_dispatch(record["dispatch_id"], owner, type(exc).__name__,
                                   backoff_seconds(record["attempts"]))
        else:
            if store.complete_dispatch(record["dispatch_id"], owner):
                delivered += 1
    return delivered

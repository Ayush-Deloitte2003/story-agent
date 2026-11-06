"""Translate Dapr workflow state into the Step 3 status response shape."""

import json

import dapr.ext.workflow as wf


def _loads(raw):
    value = json.loads(raw) if raw else None
    return json.loads(value) if isinstance(value, str) else value


def workflow_view(client, thread_id: str) -> dict | None:
    state = client.get_workflow_state(thread_id, fetch_payloads=True)
    if state is None:
        return None
    custom = _loads(state.serialized_custom_status) or {}
    runtime_status = state.runtime_status
    body: dict = {"thread_id": thread_id, "runtime_status": runtime_status.name}

    if runtime_status in (wf.WorkflowStatus.RUNNING, wf.WorkflowStatus.PENDING):
        body["status"] = custom.get("stage", "writing")
        for key in ("story", "revision", "approval_expires_at"):
            if key in custom:
                body[key] = custom[key]
    elif runtime_status == wf.WorkflowStatus.COMPLETED:
        output = _loads(state.serialized_output) or {}
        body.update(output)
        if output.get("status") == "published":
            body["reply"] = output.get("result")
    elif runtime_status == wf.WorkflowStatus.FAILED:
        details = state.failure_details
        body.update(status="failed", error=getattr(details, "message", "workflow failed"))
    else:
        body["status"] = runtime_status.name.lower()

    if isinstance(body.get("story"), dict):
        body["llm_calls"] = body["story"].pop("llm_calls", None)
    expires = body.get("approval_expires_at")
    if expires and not expires.endswith(("Z", "+00:00")):
        body["approval_expires_at"] = expires + "+00:00"
    return body

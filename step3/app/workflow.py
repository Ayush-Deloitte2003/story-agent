"""The Dapr workflow that makes the story pipeline durable.

    write_story (activity: deep agent + LLM)
        -> wait for the revision-specific approval event, or the approval timer
             reject  -> write_story again with the feedback
             timeout -> end as "expired"
             approve -> publish_story (activity: MCP tool, retried by Dapr)

Dapr records every step in its state store. If the app stops while a story is
waiting for approval, the workflow picks up where it was when the app comes
back, and the approval timer keeps counting in the meantime.
"""

import json
import logging
import os
from datetime import timedelta

import dapr.ext.workflow as wf

from app.agent import (
    check_story_continuity,
    draft_story,
    publish_via_mcp,
    review_story_brief_compliance,
    review_story_quality,
)
from app.approval import approval_event_name

log = logging.getLogger("story-agent")

# Writing calls an LLM, which can hit transient errors too.
WRITE_RETRY = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=2),
    max_number_of_attempts=3,
    backoff_coefficient=2.0,
)

QUALITY_REVIEW_RETRY = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=2),
    max_number_of_attempts=3,
    backoff_coefficient=2.0,
)

SPECIALIST_REVIEW_RETRY = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=2),
    max_number_of_attempts=3,
    backoff_coefficient=2.0,
)

# Publishing calls the flaky MCP tool: keep trying, with growing gaps.
PUBLISH_RETRY = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=float(os.getenv("PUBLISH_RETRY_INTERVAL_SECONDS", "1"))),
    max_number_of_attempts=int(os.getenv("PUBLISH_MAX_ATTEMPTS", "10")),
    backoff_coefficient=float(os.getenv("PUBLISH_RETRY_BACKOFF", "1.5")),
    max_retry_interval=timedelta(seconds=float(os.getenv("PUBLISH_MAX_RETRY_INTERVAL_SECONDS", "10"))),
)

runtime = wf.WorkflowRuntime()


def _status(ctx: wf.DaprWorkflowContext, stage: str, **fields) -> None:
    ctx.set_custom_status(json.dumps({"stage": stage, **fields}))


@runtime.workflow(name="story_workflow")
def story_workflow(ctx: wf.DaprWorkflowContext, inp: dict):
    request = inp["message"]
    timeout = timedelta(seconds=inp["approval_timeout_seconds"])
    revisions: list[dict] = []

    while True:
        _status(ctx, "writing", revision=len(revisions))
        draft = yield ctx.call_activity(
            write_story, input={"request": request, "revisions": revisions}, retry_policy=WRITE_RETRY
        )

        expires_at = ctx.current_utc_datetime + timeout
        _status(ctx, "awaiting_approval", story=draft, revision=len(revisions),
                approval_expires_at=expires_at.isoformat())

        approval = ctx.wait_for_external_event(approval_event_name(len(revisions)))
        timer = ctx.create_timer(expires_at)
        winner = yield wf.when_any([approval, timer])

        if winner == timer:
            _status(ctx, "expired", story=draft)
            return {"status": "expired", "story": draft}

        decision = approval.get_result() or {}
        if decision.get("decision") == "reject":
            revisions.append({"draft": draft, "feedback": decision.get("feedback") or "Rejected by reviewer."})
            continue

        _status(ctx, "publishing", story=draft)
        try:
            result = yield ctx.call_activity(publish_story, input=draft, retry_policy=PUBLISH_RETRY)
        except wf.TaskFailedError as exc:
            _status(ctx, "failed", story=draft)
            return {"status": "failed", "story": draft, "error": str(exc)}
        _status(ctx, "published", story=draft)
        return {"status": "published", "story": draft, "result": result}


@runtime.workflow(name="story_quality_review_workflow")
def story_quality_review_workflow(ctx: wf.DaprWorkflowContext, inp: dict):
    _status(ctx, "reviewing", title=inp["title"])
    result = yield ctx.call_activity(
        quality_review_story,
        input=inp,
        retry_policy=QUALITY_REVIEW_RETRY,
    )
    _status(ctx, "reviewed", title=inp["title"], review=result)
    return {"status": "reviewed", "title": inp["title"], "review": result}


@runtime.workflow(name="story_continuity_check_workflow")
def story_continuity_check_workflow(ctx: wf.DaprWorkflowContext, inp: dict):
    _status(ctx, "checking_continuity", title=inp["title"])
    result = yield ctx.call_activity(
        continuity_check_story,
        input=inp,
        retry_policy=SPECIALIST_REVIEW_RETRY,
    )
    _status(ctx, "continuity_checked", title=inp["title"], review=result)
    return {"status": "reviewed", "title": inp["title"], "review": result}


@runtime.workflow(name="story_brief_compliance_workflow")
def story_brief_compliance_workflow(ctx: wf.DaprWorkflowContext, inp: dict):
    _status(ctx, "checking_brief_compliance", title=inp["title"])
    result = yield ctx.call_activity(
        brief_compliance_review_story,
        input=inp,
        retry_policy=SPECIALIST_REVIEW_RETRY,
    )
    _status(ctx, "brief_compliance_checked", title=inp["title"], review=result)
    return {"status": "reviewed", "title": inp["title"], "review": result}


@runtime.activity(name="write_story")
async def write_story(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    log.info("write_story (revision %d)", len(inp["revisions"]))
    return await draft_story(inp["request"], inp["revisions"])


@runtime.activity(name="quality_review_story")
async def quality_review_story(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    log.info("quality review started for %s", inp["title"])
    return await review_story_quality(inp["title"], inp["story"], inp.get("request", ""))


@runtime.activity(name="continuity_check_story")
async def continuity_check_story(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    log.info("continuity check started for %s", inp["title"])
    return await check_story_continuity(inp["title"], inp["story"], inp.get("request", ""))


@runtime.activity(name="brief_compliance_review_story")
async def brief_compliance_review_story(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    log.info("brief-compliance review started for %s", inp["title"])
    return await review_story_brief_compliance(
        inp["title"], inp["story"], inp.get("request", "")
    )


@runtime.activity(name="publish_story")
async def publish_story(ctx: wf.WorkflowActivityContext, draft: dict) -> str:
    try:
        result = await publish_via_mcp(draft["title"], draft["story"], idempotency_key=ctx.workflow_id)
    except Exception as exc:
        log.warning("publish_story failed, Dapr will retry: %s", exc)
        raise
    log.info("publish_story succeeded: %s", result)
    return result

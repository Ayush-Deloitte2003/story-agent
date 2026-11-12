"""Dapr workflow worker dedicated to continuity-check events."""

import asyncio
import json
import logging
import os
from datetime import timedelta

import dapr.ext.workflow as wf

from app.events import (
    parse_story_continuity_check_event,
    workflow_id_for_continuity_check_event,
)
from app.specialist_worker_app import create_specialist_app
from app.specialist_agent_client import review_with_sidecar

log = logging.getLogger("story-agent.specialist")
runtime = wf.WorkflowRuntime()
RETRY = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=2),
    max_number_of_attempts=3,
    backoff_coefficient=2.0,
)


@runtime.workflow(name="story_continuity_check_workflow")
def story_continuity_check_workflow(ctx: wf.DaprWorkflowContext, inp: dict):
    ctx.set_custom_status(json.dumps({"stage": "checking_continuity", "title": inp["title"]}))
    result = yield ctx.call_activity(
        continuity_check_story, input=inp, retry_policy=RETRY
    )
    ctx.set_custom_status(json.dumps({
        "stage": "continuity_checked",
        "title": inp["title"],
        "review": result,
    }))
    return {"status": "reviewed", "title": inp["title"], "review": result}


@runtime.activity(name="continuity_check_story")
async def continuity_check_story(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    log.info("continuity check started for %s", inp["title"])
    await asyncio.sleep(float(os.getenv("SPECIALIST_ACTIVITY_DELAY_SECONDS", "0")))
    return await review_with_sidecar(
        inp["title"], inp["story"], inp.get("request", "")
    )


app = create_specialist_app(
    name="continuity-check",
    event_type="story.continuity-check.requested",
    parser=parse_story_continuity_check_event,
    workflow_id_builder=workflow_id_for_continuity_check_event,
    workflow=story_continuity_check_workflow,
    runtime=runtime,
)

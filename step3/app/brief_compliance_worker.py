"""Dapr workflow worker dedicated to brief-compliance events."""

import asyncio
import json
import logging
import os
from datetime import timedelta

import dapr.ext.workflow as wf

from app.agent import review_story_brief_compliance
from app.events import (
    parse_story_brief_compliance_event,
    workflow_id_for_brief_compliance_event,
)
from app.specialist_worker_app import create_specialist_app

log = logging.getLogger("story-agent.specialist")
runtime = wf.WorkflowRuntime()
RETRY = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=2),
    max_number_of_attempts=3,
    backoff_coefficient=2.0,
)


@runtime.workflow(name="story_brief_compliance_workflow")
def story_brief_compliance_workflow(ctx: wf.DaprWorkflowContext, inp: dict):
    ctx.set_custom_status(json.dumps({
        "stage": "checking_brief_compliance",
        "title": inp["title"],
    }))
    result = yield ctx.call_activity(
        brief_compliance_review_story, input=inp, retry_policy=RETRY
    )
    ctx.set_custom_status(json.dumps({
        "stage": "brief_compliance_checked",
        "title": inp["title"],
        "review": result,
    }))
    return {"status": "reviewed", "title": inp["title"], "review": result}


@runtime.activity(name="brief_compliance_review_story")
async def brief_compliance_review_story(ctx: wf.WorkflowActivityContext, inp: dict) -> dict:
    log.info("brief-compliance review started for %s", inp["title"])
    await asyncio.sleep(float(os.getenv("SPECIALIST_ACTIVITY_DELAY_SECONDS", "0")))
    return await review_story_brief_compliance(
        inp["title"], inp["story"], inp.get("request", "")
    )


app = create_specialist_app(
    name="brief-compliance",
    event_type="story.brief-compliance.requested",
    parser=parse_story_brief_compliance_event,
    workflow_id_builder=workflow_id_for_brief_compliance_event,
    workflow=story_brief_compliance_workflow,
    runtime=runtime,
)

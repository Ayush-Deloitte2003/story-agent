"""Shared HTTP and Pub/Sub surface for independently deployed specialist workers."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException

from app.workflow_status import workflow_view

log = logging.getLogger("story-agent.specialist")


def create_specialist_app(
    *,
    name: str,
    event_type: str,
    parser,
    workflow_id_builder,
    workflow,
    runtime,
) -> FastAPI:
    client = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal client
        runtime.start()
        from dapr.ext.workflow import DaprWorkflowClient

        client = DaprWorkflowClient()
        log.info("%s workflow runtime started", name)
        yield
        runtime.shutdown()

    app = FastAPI(title=f"Story agent: {name} worker", lifespan=lifespan)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok", "worker": name}

    @app.get("/dapr/subscribe", include_in_schema=False)
    async def dapr_subscriptions():
        return [{
            "pubsubname": "pubsub",
            "topic": "story-events",
            "routes": {
                "rules": [{
                    "match": f'event.type == "{event_type}"',
                    "path": "/events",
                }],
                "default": "/events/unhandled",
            },
            "deadLetterTopic": "story-events-dead",
        }]

    @app.post("/events", include_in_schema=False)
    async def receive_event(event: object = Body(...)):
        try:
            source, event_id, workflow_input = parser(event)
        except ValueError as exc:
            log.warning("Dropping invalid %s event: %s", name, exc)
            return {"status": "DROP", "message": str(exc)}

        workflow_id = workflow_id_builder(source, event_id)
        try:
            await asyncio.to_thread(
                client.schedule_new_workflow,
                workflow,
                input=workflow_input,
                instance_id=workflow_id,
            )
        except Exception:
            existing = await asyncio.to_thread(
                client.get_workflow_state, workflow_id, fetch_payloads=False
            )
            if existing is None:
                log.exception("Could not start %s workflow for event %s/%s", name, source, event_id)
                return {"status": "RETRY"}
            log.info("Acknowledging redelivered %s event %s/%s", name, source, event_id)
        else:
            log.info("Started %s workflow %s from event %s/%s", name, workflow_id, source, event_id)
        return {"status": "SUCCESS", "thread_id": workflow_id}

    @app.post("/events/unhandled", include_in_schema=False)
    async def unhandled_event(event: object = Body(...)):
        event_type_value = event.get("type") if isinstance(event, dict) else None
        log.warning("Dropping event with no matching %s route: %s", name, event_type_value)
        return {"status": "DROP"}

    @app.get("/events/{event_id}", include_in_schema=False)
    async def event_status(event_id: str, source: str):
        workflow_id = workflow_id_builder(source, event_id)
        view = await asyncio.to_thread(workflow_view, client, workflow_id)
        if view is None:
            raise HTTPException(404, f"No {name} workflow found for this event.")
        return view

    return app

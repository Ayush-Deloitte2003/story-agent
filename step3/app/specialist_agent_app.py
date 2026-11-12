"""HTTP surface for a specialist agent running as a pod sidecar container."""

import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, Field

log = logging.getLogger("story-agent.specialist")


class SpecialistReviewRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    story: str = Field(min_length=1, max_length=20000)
    request: str = Field("", max_length=2000)


class QualityReviewRequest(BaseModel):
    title: str
    story: str
    request: str = ""


ReviewFunction = Callable[[str, str, str], Awaitable[dict]]


def create_specialist_agent_app(
    name: str,
    review: ReviewFunction,
    request_model: type[BaseModel] = SpecialistReviewRequest,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("%s agent sidecar started", name)
        yield
        log.info("%s agent sidecar stopped", name)

    app = FastAPI(title=f"Story agent: {name}", lifespan=lifespan)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok", "agent": name}

    @app.post("/review", include_in_schema=False)
    async def review_story(inp: request_model):
        return await review(inp.title, inp.story, inp.request)

    return app

"""Story-writing agent served as a container beside the Dapr workflow host."""

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from pydantic import BaseModel

from app.agent import draft_story

log = logging.getLogger("story-agent.writer")


class DraftRequest(BaseModel):
    request: str
    revisions: list[dict]


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("story writer agent sidecar started")
    yield
    log.info("story writer agent sidecar stopped")


app = FastAPI(title="Story writer agent", lifespan=lifespan)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok", "agent": "story-writer"}


@app.post("/draft", include_in_schema=False)
async def create_draft(inp: DraftRequest):
    return await draft_story(inp.request, inp.revisions)

"""A bounded, deterministic recommendation sidecar with no booking side effects."""

from fastapi import FastAPI
from pydantic import BaseModel

from app.inventory import recommend_alternatives

app = FastAPI(title="Flight recommendation sidecar")


class RecommendationRequest(BaseModel):
    flight_id: str


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/recommend")
def recommend(request: RecommendationRequest):
    return {"alternatives": recommend_alternatives(request.flight_id)}

"""Quality reviewer served as a container beside the Dapr workflow host."""

from app.agent import review_story_quality
from app.specialist_agent_app import QualityReviewRequest, create_specialist_agent_app

app = create_specialist_agent_app(
    "quality-review",
    review_story_quality,
    request_model=QualityReviewRequest,
)

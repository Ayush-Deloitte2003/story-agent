"""Brief-compliance reviewer served beside its Dapr workflow host."""

from app.agent import review_story_brief_compliance
from app.specialist_agent_app import create_specialist_agent_app

app = create_specialist_agent_app("brief-compliance", review_story_brief_compliance)

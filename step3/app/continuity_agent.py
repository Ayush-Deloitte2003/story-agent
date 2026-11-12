"""Continuity checker served as a container beside its Dapr workflow host."""

from app.agent import check_story_continuity
from app.specialist_agent_app import create_specialist_agent_app

app = create_specialist_agent_app("continuity-check", check_story_continuity)

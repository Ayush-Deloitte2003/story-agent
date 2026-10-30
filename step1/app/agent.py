"""The deep agent: loads the story skill, writes a story, asks to publish it.

The model is configured the same way as the reference LangGraph example
(`ChatLiteLLM(model=...)`), so swapping providers is a one-line env change.
"""

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.tools import tool
from langchain_litellm import ChatLiteLLM
from langgraph.checkpoint.memory import InMemorySaver

BASE_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = BASE_DIR / "skills"
PUBLISH_DIR = BASE_DIR / "published"

# Name of the tool that pauses for human approval. main.py reads the pending
# story out of this tool call's arguments.
PUBLISH_TOOL = "publish_story"

SYSTEM_PROMPT = (
    "You are a story-writing assistant. When the user asks for a story, read "
    "the matching skill first and follow it exactly. Publishing always goes "
    f"through the `{PUBLISH_TOOL}` tool, which a human approves or rejects."
)


@tool
def publish_story(title: str, story: str) -> str:
    """Publish a finished story. A human must approve this call before it runs.

    Args:
        title: Short title for the story.
        story: The complete story text.
    """
    PUBLISH_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "story"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = PUBLISH_DIR / f"{stamp}-{slug}.md"
    path.write_text(f"# {title}\n\n{story}\n", encoding="utf-8")
    return f"Published '{title}' to {path.relative_to(BASE_DIR)}"


def build_agent():
    model = ChatLiteLLM(
        model=os.getenv("LLM_MODEL", "openai/gpt-4o"),
        temperature=float(os.getenv("LLM_TEMPERATURE", "0.7")),
    )
    return create_deep_agent(
        model=model,
        tools=[publish_story],
        system_prompt=SYSTEM_PROMPT,
        # The agent's filesystem is rooted at ./skills and is read-only, so it
        # can load skills but cannot see .env or change anything on disk.
        backend=FilesystemBackend(root_dir=SKILLS_DIR, virtual_mode=True),
        skills=["/"],
        permissions=[
            FilesystemPermission(
                operations=["write"],
                # wcmatch's "**" skips dotfiles, so they are listed explicitly.
                paths=["/**", "/.*", "/.*/**", "/**/.*", "/**/.*/**"],
                mode="deny",
            ),
        ],
        # Human-in-the-loop: pause before publish_story runs.
        interrupt_on={PUBLISH_TOOL: {"allowed_decisions": ["approve", "reject"]}},
        # Step 1 keeps paused runs in memory, so they are lost on restart.
        # Step 3 replaces this with a durable runtime.
        checkpointer=InMemorySaver(),
    )

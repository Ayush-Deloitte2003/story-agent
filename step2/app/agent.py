"""The deep agent: loads the story skill, writes a story, asks to publish it.

Publishing is done by the `publish_story` tool of a separate MCP server
(mcp_server/publisher.py). The agent pauses for human approval before that
tool runs. The model is configured as in the reference LangGraph example
(`ChatLiteLLM(model=...)`).
"""

import os
from pathlib import Path

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_litellm import ChatLiteLLM
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import InMemorySaver

BASE_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = BASE_DIR / "skills"

# Name of the MCP tool that pauses for human approval. main.py reads the
# pending story out of this tool call's arguments.
PUBLISH_TOOL = "publish_story"

SYSTEM_PROMPT = (
    "You are a story-writing assistant. When the user asks for a story, read "
    "the matching skill first and follow it exactly. Publishing always goes "
    f"through the `{PUBLISH_TOOL}` tool, which a human approves or rejects."
)


def mcp_url() -> str:
    return os.getenv("MCP_URL", "http://127.0.0.1:8765/mcp")


async def load_mcp_tools():
    """Fetch the tools the publisher MCP server offers."""
    client = MultiServerMCPClient(
        {"publisher": {"transport": "streamable_http", "url": mcp_url()}},
        # Let tool failures raise instead of being handed back to the LLM as
        # text, so a failed publish stops the run. Step 3 adds the retries.
        handle_tool_errors=False,
    )
    tools = await client.get_tools()
    if PUBLISH_TOOL not in {t.name for t in tools}:
        raise RuntimeError(f"MCP server at {mcp_url()} does not offer `{PUBLISH_TOOL}`")
    return tools


def build_agent(tools):
    model = ChatLiteLLM(
        model=os.getenv("LLM_MODEL", "openai/gpt-4o"),
        temperature=float(os.getenv("LLM_TEMPERATURE", "0.7")),
    )
    return create_deep_agent(
        model=model,
        tools=tools,
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
        # Paused runs are kept in memory, so they are lost on restart.
        # Step 3 replaces this with a durable runtime.
        checkpointer=InMemorySaver(),
    )

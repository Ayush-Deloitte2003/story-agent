> **On Windows, use the commands in the README one folder up (`.\run ...`).** This file explains the code.

# Story agent: step 1

A deep agent behind FastAPI. `/chat` loads the story skill, has the LLM write
a story, and pauses for a human to approve or reject before publishing.

```
POST /chat ──> deep agent ──> reads skills/hp-season-1/SKILL.md
                          ──> LLM writes the story, calls publish_story
                          ──> PAUSE (human approval, 24h window)
POST /chat/{thread_id}/decision ──> approve: story saved to published/
                                ──> reject + feedback: agent revises, pauses again
```

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # paste your OpenAI key into .env
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 for a small review page, or use the API:

```bash
curl -X POST localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message": "Write the season 1 story."}'
# -> {"thread_id": "...", "status": "awaiting_approval", "story": {...}, "llm_calls": 2}

curl -X POST localhost:8000/chat/<thread_id>/decision -H 'content-type: application/json' \
  -d '{"decision": "approve"}'
# or: -d '{"decision": "reject", "feedback": "Make it funnier."}'

curl localhost:8000/chat/<thread_id>     # check status any time
```

The server log prints one line per LLM call.

## Layout

| File | What it does |
| --- | --- |
| `app/agent.py` | `create_deep_agent` with the model, the `publish_story` tool, the skill folder and the approval pause |
| `app/main.py` | FastAPI endpoints, approval window, LLM call counter |
| `app/index.html` | Review page: shows the story, approve / reject buttons |
| `skills/hp-season-1/SKILL.md` | The skill the agent follows to write the story |

## Settings (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENAI_API_KEY` | none | Your key |
| `LLM_MODEL` | `openai/gpt-4o` | LiteLLM model string, same style as the reference example's `sap/gpt-4o` |
| `LLM_TEMPERATURE` | `0.7` | |
| `APPROVAL_TIMEOUT_HOURS` | `24` | After this, a pending story can no longer be approved |

## Known limits, on purpose

- Paused runs and approval deadlines live in memory, so a restart loses them
  and the app must run as a single worker. Step 3 replaces this with a
  durable runtime.
- `publish_story` just writes a markdown file. Step 2 swaps it for the MCP
  tool that fails.

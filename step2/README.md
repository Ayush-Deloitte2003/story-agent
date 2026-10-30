> **On Windows, use the commands in the README one folder up (`.\run ...`).** This file explains the code.

# Story agent (Steps 1 and 2)

A deep agent behind FastAPI. `/chat` loads the story skill, has the LLM write
a story, and pauses for a human to approve it. Publishing then goes through a
separate **MCP server** whose `publish_story` tool is deliberately flaky: each
story fails its first 7 attempts with a `RuntimeError`, then succeeds.

```
POST /chat ──> deep agent ──> reads skills/hp-season-1/SKILL.md
                          ──> LLM writes the story, calls publish_story
                          ──> PAUSE (human approval, 24h window)
POST /chat/{thread_id}/decision
   reject + feedback ──> agent revises, pauses again
   approve ──> MCP server: publish_story ──> RuntimeError (attempts 1–7)
                                         ──> published/<story>.md (attempt 8)
```

In Step 2 nothing retries: the first `RuntimeError` stops the run with status
`failed`, and the human's approval is lost. Step 3 adds a durable runtime
(Temporal, Dapr or DBOS) that retries the tool until it succeeds.

## Setup (once)

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt httpx
```

Create `.env` in this folder (next to `.env.example`) with at least:

```
OPENAI_API_KEY=sk-your-key
```

## Verify

```powershell
.venv\Scripts\python scripts\verify_step2.py fake   # stand-in model, no key needed
.venv\Scripts\python scripts\verify_step2.py real   # your OpenAI key
```

The script starts its own MCP servers and prints PASS/FAIL for:

- **Part A:** Step 1 still works, and approving publishes through a healthy MCP server.
- **Part B:** with the flaky MCP server, approving ends in `failed` with the
  `RuntimeError`, nothing is published, and the approval can't be reused.
- **Part C:** calling the flaky tool directly 8 times gives 7 failures, then success.

Reports are saved to `verify_output_step2_fake.txt` and `verify_output_step2_real.txt`.

## Run it yourself (two terminals)

Terminal 1, the MCP server:

```powershell
.venv\Scripts\python mcp_server\publisher.py
```

Terminal 2, the app:

```powershell
.venv\Scripts\python -m uvicorn app.main:app
```

Open http://127.0.0.1:8000, click **Write story**, then **Approve and publish**.
The page shows the publish failure, and terminal 1 logs `attempt 1 of 8: FAIL`.
To see a successful publish, set `MCP_FAIL_TIMES=0` in `.env` and restart terminal 1.

## Layout

| File | What it does |
| --- | --- |
| `app/agent.py` | `create_deep_agent` with the model, MCP tools, the skill folder and the approval pause |
| `app/main.py` | FastAPI endpoints; loads MCP tools at startup; reports `failed` runs |
| `app/index.html` | Review page: shows the story, approve / reject buttons, errors |
| `mcp_server/publisher.py` | Flaky MCP server exposing `publish_story` |
| `skills/hp-season-1/SKILL.md` | The skill the agent follows to write the story |
| `scripts/verify_step2.py` | Automated PASS/FAIL check of Steps 1 and 2 |

## Settings (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENAI_API_KEY` | none | Your key |
| `LLM_MODEL` | `openai/gpt-4o` | LiteLLM model string, same style as the reference example's `sap/gpt-4o` |
| `LLM_TEMPERATURE` | `0.7` | |
| `APPROVAL_TIMEOUT_HOURS` | `24` | After this, a pending story can no longer be approved |
| `MCP_URL` | `http://127.0.0.1:8765/mcp` | Where the app finds the publisher MCP server |
| `MCP_FAIL_TIMES` | `7` | Failed attempts per story before publishing succeeds |
| `MCP_PORT` | `8765` | Port the publisher MCP server listens on |

## Known limits, on purpose

- No retries: one tool failure ends the run. Step 3's job.
- Paused runs and approval deadlines live in memory, so a restart of the app
  loses them, and it must run as a single worker. Step 3's job too.
- The MCP server keeps attempt counts in `mcp_server/attempts.json`, so they
  survive its restarts. Delete that file to reset them.

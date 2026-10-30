> **On Windows, use the commands in the README one folder up (`.\run ...`).** This file explains the code.

# Story agent (Step 3: durable with Dapr)

A deep agent behind FastAPI writes a story, a human approves it, and a flaky
MCP tool publishes it. **Dapr Workflow** makes the whole thing durable:

```
POST /chat ──> Dapr workflow "story_workflow"
                 1. activity write_story ── deep agent reads SKILL.md, LLM writes the story,
                                            proposes publish_story(title, story)
                 2. durable wait for the "approval" event   ⏳ (APPROVAL_TIMEOUT_HOURS, default 24h)
                      reject + feedback ──> back to 1 with the feedback
                      timer fires first ──> ends as "expired"
                 3. activity publish_story ── MCP tool on the publisher server
                      RuntimeError ──> Dapr retries with backoff (1s, 1.5s, 2.3s … up to 10s)
                      attempt 8 succeeds ──> ends as "published"
POST /chat/{thread_id}/decision ──> raises the "approval" event on the workflow
GET  /chat/{thread_id}          ──> reads the workflow's state from Dapr
```

Every step is recorded in Dapr's state store, so:

- a story waiting for approval survives the app being stopped or restarted,
  and the approval timer keeps running while it is down;
- if the app dies partway through the publish retries, they continue where
  they stopped when it comes back;
- if the retries run out (`PUBLISH_MAX_ATTEMPTS`), the run ends as `failed`
  with the last error instead of hanging.

The deep agent decides *what* to publish (it proposes the `publish_story`
call); Dapr owns the waiting and the retried execution of that call.

## Option 1: run locally with Dapr (quickest)

Needs Docker Desktop running and the Dapr CLI (`winget install Dapr.CLI`).

```powershell
dapr init                                   # once: starts Dapr's placement + scheduler in Docker
.venv\Scripts\python -m pip install -r requirements.txt
```

Terminal 1, the flaky MCP server:

```powershell
.venv\Scripts\python mcp_server\publisher.py
```

Terminal 2, the app with a Dapr sidecar (state kept in `dapr\state.db`):

```powershell
dapr run --app-id story-agent --app-port 8000 --resources-path .\dapr\components -- .venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000, write a story, approve it, and watch terminal 1
log attempts 1–7 failing and attempt 8 publishing.

## Option 2: run in a Kind cluster (as in the task diagram)

Needs Docker Desktop, `kind`, `kubectl` and the Dapr CLI:

```powershell
winget install Kubernetes.kind Kubernetes.kubectl Dapr.CLI
powershell -ExecutionPolicy Bypass -File k8s\deploy-kind.ps1
kubectl port-forward svc/story-agent 8000:8000
```

The script creates a Kind cluster `story`, installs Dapr into it, builds the
image, loads it into Kind, turns your `.env` into a Kubernetes secret and
applies `k8s/`: Redis (state store), the Dapr `statestore` component, the
publisher MCP server, and the app with an injected Dapr sidecar.

Useful commands:

```powershell
kubectl get pods                                     # all should be Running (story-agent shows 2/2: app + Dapr sidecar)
kubectl logs deploy/publisher -f                     # watch the publish attempts
kubectl logs deploy/story-agent -c story-agent -f    # the app
kubectl exec deploy/publisher -- ls /data/published  # published stories
```

Restart test in Kind: run `verify_step3.py start`, then `kubectl delete pod -l app=story-agent`
and wait for `kubectl get pods` to show the new pod `2/2 Running`. The port-forward stops when
its pod goes away, so start `kubectl port-forward svc/story-agent 8000:8000` again, then run
`verify_step3.py resume`. The story survives because its workflow history is in Redis, not in the pod.

If a pod is stuck: `kubectl describe pod -l app=<name>` shows why (image not loaded,
secret missing, sidecar not injected). To start over: `kind delete cluster --name story`.

## Verify

With the app running (either option):

```powershell
.venv\Scripts\python scripts\verify_step3.py          # revise, retries, expiry
.venv\Scripts\python scripts\verify_step3.py start    # leave a story awaiting approval
#   ...stop and restart the app (Ctrl+C + dapr run again, or delete the pod in Kind)...
.venv\Scripts\python scripts\verify_step3.py resume   # it is still there; approve and publish
```

Reports are saved as `verify_output_step3_*.txt`.

## Layout

| File | What it does |
| --- | --- |
| `app/workflow.py` | The Dapr workflow, its two activities and the retry policies |
| `app/agent.py` | Deep agent (skill + LLM) that drafts the story; MCP client for publishing |
| `app/main.py` | FastAPI endpoints on top of the Dapr workflow client |
| `app/index.html` | Review page: story, approve / reject, live publishing status |
| `mcp_server/publisher.py` | Flaky MCP server: each story fails 7 times, then publishes |
| `skills/hp-season-1/SKILL.md` | The skill the agent follows to write the story |
| `dapr/components/statestore.yaml` | Local state store (SQLite) for `dapr run` |
| `k8s/` | Kind manifests and `deploy-kind.ps1` |
| `Dockerfile` | One image for the app and the publisher |
| `scripts/verify_step3.py` | PASS/FAIL checks against the running app |

## Settings (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENAI_API_KEY` | none | Your key |
| `LLM_MODEL` | `openai/gpt-4o` | LiteLLM model string |
| `APPROVAL_TIMEOUT_HOURS` | `24` | Approval window (a Dapr durable timer). `POST /chat` can override it per story with `approval_timeout_seconds` |
| `PUBLISH_MAX_ATTEMPTS` | `10` | Attempts before the workflow gives up and ends as `failed` |
| `PUBLISH_RETRY_INTERVAL_SECONDS` | `1` | Wait before the first retry |
| `PUBLISH_RETRY_BACKOFF` | `1.5` | Each wait is this much longer than the last |
| `PUBLISH_MAX_RETRY_INTERVAL_SECONDS` | `10` | Longest wait between retries |
| `MCP_URL` | `http://127.0.0.1:8765/mcp` | Where the app finds the publisher (set for you in Kind) |
| `MCP_FAIL_TIMES` | `7` | Failed attempts per story before publishing succeeds |

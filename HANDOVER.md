# Handover: Durable Story Agent (Steps 1–3)

> **For the AI coding assistant (GitHub Copilot) picking this up.** Read this whole file
> before changing anything. It describes the assignment, what is built and verified, the
> exact environment constraints on the developer's laptop, and the remaining work with
> acceptance criteria. Last updated: 30 October 2026.

---

## 1. The assignment

The original task (from a whiteboard diagram) has three steps:

| Step | Requirement |
| --- | --- |
| 1 | Run a **deep agent** behind **FastAPI** (`/chat`). It invokes a **skill** (`SKILL.md`, "Season 1: Harry Potter movie 1"), makes an LLM call to write a story, displays it, and **waits for human approval** before publishing (24h window). |
| 2 | Add an **MCP tool that throws a runtime exception** and integrate it into the deep agent. |
| 3 | Connect a durable framework (**Dapr was chosen**, the diagram says "Dapr (Kind Cluster)"; Temporal and DBOS were the alternatives) so the MCP tool is **retried ~7–8 times** and the **human-approval wait is durable for a configurable time**. |

The reference model setup came from
`sap-contributions/litellm-agentic-examples/langgraph_example/langgraph_agent.py`, which uses
`ChatLiteLLM(model=...)`. We use the same, with `openai/gpt-4o` and an OpenAI key.

---

## 2. Status at handover

| Item | Status | Evidence |
| --- | --- | --- |
| Step 1: deep agent + FastAPI + skill + human approval | **Done, verified** | `step1\verify_output_real.txt` (GPT-4o, all checks passed) |
| Step 2: flaky MCP tool integrated | **Done, verified** | Shown live on the laptop; automated checks (`step2\scripts\verify_step2.py`) passed in the development environment |
| Step 3: Dapr Workflow, retries | **Done, verified** | `step3\verify_output_step3_run.txt`: published on attempt 8 |
| Step 3: durable, configurable approval wait | **Done, verified** | Same file, Part B: 10 s window expired, late approval → HTTP 410 |
| Step 3: survives app restart | **Done, verified** | `step3\verify_output_step3_start.txt` + `..._resume.txt` |
| **Step 3 on a Kind (Kubernetes) cluster** | **NOT DONE: written but never run** | `step3\k8s\*`, `step3\Dockerfile`; blocked by no Docker on the laptop |

**The remaining work is Section 8 (Kind deployment).** Section 9 lists optional follow-ups.

---

## 3. Repository layout

The working copy is the folder that contains `run.cmd` (on the developer's laptop:
`C:\Users\aykumar17\Downloads\story-agent-all-steps1-...\story-agent-all-steps\story-agent-all-steps`).

```
story-agent-all-steps/
├── run.cmd                    # entry point: `.\run <action>` → scripts\runner.ps1
├── scripts/runner.ps1         # setup, demos, verification, restart test, Kind deploy, stop
├── README.md                  # user-facing run instructions (Windows/PowerShell)
├── HANDOVER.md                # this file
├── .venv/                     # ONE shared Python venv for all steps (created by setup)
├── .env                       # OPENAI_API_KEY etc.; copied into step1/, step2/, step3/  (never commit)
├── tools/dapr/dapr.exe        # Dapr CLI, downloaded by setup (winget is blocked)
├── step1/                     # SNAPSHOT of Step 1. Do not modify
├── step2/                     # SNAPSHOT of Step 2. Do not modify
└── step3/                     # CURRENT project. All remaining work happens here
```

`step1` and `step2` are frozen snapshots kept so each step can be demonstrated as it was.
**Only change `step3/` and `scripts/runner.ps1`** unless asked otherwise.

### step3/ in detail

| Path | Purpose |
| --- | --- |
| `app/workflow.py` | **Core.** Dapr workflow `story_workflow` + activities `write_story`, `publish_story`; retry policies |
| `app/agent.py` | Deep agent (`create_deep_agent`), `draft_story()` (runs the agent until it proposes `publish_story`), `publish_via_mcp()` (one MCP call), `load_mcp_tools()` |
| `app/main.py` | FastAPI app; starts the Dapr `WorkflowRuntime` in `lifespan`; REST API (Section 5) |
| `app/index.html` | Minimal HTML/JS review page (Write story / Approve / Reject; polls status) |
| `mcp_server/publisher.py` | MCP server (FastMCP from the official `mcp` SDK, streamable HTTP) exposing `publish_story`; fails the first `MCP_FAIL_TIMES` attempts **per story** |
| `skills/hp-season-1/SKILL.md` | The skill the agent loads |
| `dapr/components/statestore.yaml` | Local state store: `state.sqlite`, file `dapr/state.db`, `actorStateStore: true` |
| `scripts/verify_step3.py` | HTTP-level checks against the running app: modes `run`, `start`, `resume`; `--url` option |
| `Dockerfile` | One image (`story-agent:dev`) for both app and MCP server |
| `k8s/redis.yaml` | Redis Deployment + PVC (`redis-data`, 1Gi) + Service |
| `k8s/statestore.yaml` | Dapr Component `statestore`, `state.redis`, `redisHost: redis:6379`, `actorStateStore: "true"` |
| `k8s/publisher.yaml` | MCP server Deployment + Service `publisher:8765` (no Dapr sidecar) |
| `k8s/story-agent.yaml` | App Deployment with Dapr annotations (`app-id story-agent`, `app-port 8000`), Service `story-agent:8000`, `envFrom` secret `story-agent-env`, `MCP_URL=http://publisher:8765/mcp` |
| `k8s/deploy-kind.ps1` | Creates Kind cluster `story`, `dapr init -k`, builds/loads image, creates secret from `.env`, applies manifests |

---

## 4. Architecture (Step 3)

```
Browser / verify script
        │ HTTP :8000
        ▼
FastAPI app (app/main.py) ──gRPC──► Dapr sidecar (daprd) ──► State store (SQLite local / Redis in Kind)
  ├─ Workflow runtime (story_workflow)          │
  └─ Deep agent (deepagents + ChatLiteLLM)       └──► Placement :50005 + Scheduler :50006 (timers, reminders)
        │ HTTPS                │ HTTP (MCP streamable HTTP)
        ▼                      ▼
   OpenAI GPT-4o        Publisher MCP server :8765/mcp (fails 7×, then OK)
```

### The workflow (`app/workflow.py`)

```
loop:
  draft = call_activity(write_story, {request, revisions}, retry=WRITE_RETRY)
  set_custom_status({"stage": "awaiting_approval", story, revision, approval_expires_at})
  winner = when_any([wait_for_external_event("approval"), create_timer(expires_at)])
  timer won   → return {"status": "expired", story}
  reject      → revisions.append({draft, feedback}); continue
  approve     → call_activity(publish_story, draft, retry=PUBLISH_RETRY)
                  success → {"status": "published", story, result}
                  TaskFailedError → {"status": "failed", story, error}
```

- **Design decision:** the deep agent only *proposes* `publish_story` (it is configured with
  `interrupt_on={"publish_story": ...}`; `draft_story()` runs it to that interrupt and returns
  the proposed `title`/`story`). **Dapr executes** the publish via the `publish_story` activity,
  so Dapr's retry policy applies to exactly that call.
- `WRITE_RETRY`: 2 s first interval, ×2 backoff, max 3 attempts.
- `PUBLISH_RETRY`: env-configurable; defaults 1 s first interval, ×1.5 backoff, 10 s max interval, max 10 attempts.
- MCP client uses `handle_tool_errors=False` so tool failures raise (and Dapr retries them).
- Custom status `stage` values: `writing`, `awaiting_approval`, `publishing`, `published`, `expired`, `failed`.

---

## 5. REST API (`app/main.py`)

| Method | Path | Body | Behaviour |
| --- | --- | --- | --- |
| GET | `/` | — | Serves `index.html` |
| POST | `/chat` | `{"message": str, "approval_timeout_seconds"?: float}` | Schedules `story_workflow` (instance id = `thread_id`), waits until stage ≠ `writing`, returns view |
| POST | `/chat/{thread_id}/decision` | `{"decision": "approve"\|"reject", "feedback"?: str}` | 404 unknown; 410 if expired; 409 if not awaiting approval; else raises `approval` event. Reject → waits for next revision; approve → returns once publishing started |
| GET | `/chat/{thread_id}` | — | Current view: `thread_id, runtime_status, status, story{title,story}, revision, approval_expires_at, llm_calls, reply/result, error` |

---

## 6. Configuration (`.env`, also turned into the K8s secret)

| Variable | Default | Used by |
| --- | --- | --- |
| `OPENAI_API_KEY` | — | LiteLLM (required) |
| `LLM_MODEL` | `openai/gpt-4o` | `app/agent.py` |
| `LLM_TEMPERATURE` | `0.7` | `app/agent.py` |
| `APPROVAL_TIMEOUT_HOURS` | `24` | `app/main.py` (per-request override: `approval_timeout_seconds`) |
| `WAIT_SECONDS` | `120` | `app/main.py`: how long HTTP calls wait for the next stage |
| `MCP_URL` | `http://127.0.0.1:8765/mcp` | `app/agent.py` (K8s: `http://publisher:8765/mcp`) |
| `PUBLISH_MAX_ATTEMPTS` / `PUBLISH_RETRY_INTERVAL_SECONDS` / `PUBLISH_RETRY_BACKOFF` / `PUBLISH_MAX_RETRY_INTERVAL_SECONDS` | `10` / `1` / `1.5` / `10` | `app/workflow.py` |
| `MCP_FAIL_TIMES`, `MCP_HOST`, `MCP_PORT`, `MCP_STATE_FILE`, `PUBLISH_DIR` | `7`, `127.0.0.1`, `8765`, `mcp_server/attempts.json`, `published/` | `mcp_server/publisher.py` |

Versions in use: Python 3.11+ (laptop has 3.x via `python`), `deepagents` 0.7, `langchain-mcp-adapters` 0.3,
`mcp` 1.30, `dapr`/`dapr-ext-workflow` 1.18, Dapr CLI 1.18.2, Dapr runtime 1.18 (slim).

---

## 7. How it runs today (local, self-hosted Dapr in **slim** mode)

All commands from the folder containing `run.cmd`, in PowerShell:

```powershell
.\run setup               # venv, packages, .env copy, Dapr CLI download, `dapr init --slim`
.\run step3-demo          # opens windows: Dapr placement, Dapr scheduler, MCP server, app (dapr run); opens browser
.\run step3-verify        # verify_step3.py run   → ALL CHECKS PASSED
.\run step3-restart-test  # start → dapr stop + relaunch app → resume → ALL CHECKS PASSED
.\run stop
```

What `runner.ps1` launches for Step 3 (useful if running by hand):

```powershell
& "$env:USERPROFILE\.dapr\bin\placement.exe" --port 50005 --healthz-port 8093 --metrics-port 9094
& "$env:USERPROFILE\.dapr\bin\scheduler.exe" --port 50006 --etcd-data-dir "$env:USERPROFILE\.dapr\scheduler-data" --healthz-port 8091 --metrics-port 9092 --override-broadcast-host-port localhost:50006
..\.venv\Scripts\python mcp_server\publisher.py              # from step3\
dapr run --app-id story-agent --app-port 8000 --dapr-http-port 3500 --dapr-grpc-port 50001 --resources-path .\dapr\components --placement-host-address localhost:50005 --scheduler-host-address localhost:50006 -- ..\.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

### Laptop constraints (important: these caused every past failure)

1. **Windows, PowerShell 5.1, corporate-managed.** `winget` is **disabled by Group Policy**; assume **no admin rights**.
2. **Port 8080 is taken** by another program → placement uses `--healthz-port 8093`. Avoid 8080/9090 for anything new.
3. PowerShell 5.1 treats a native program's stderr as fatal under `$ErrorActionPreference = "Stop"`. Scripts use `"Continue"` and check `$LASTEXITCODE`.
4. Files written by PowerShell must be **UTF-8 without BOM** (python-dotenv misreads a BOM).
5. `.env` saved by VS Code on Windows has CRLF; parse it yourself (see `deploy-kind.ps1`) rather than `kubectl --from-env-file`.
6. PowerShell unrolls single-element arrays on `return`: use `return ,@(...)`.
7. The VS Code terminal may auto-activate `.venv`; that's harmless. Scripts call `.venv\Scripts\python` explicitly.
8. **Docker Desktop is not installed** and likely cannot be without IT approval. This is why Kind is not done.

---

## 8. REMAINING WORK: run Step 3 on Kind (Kubernetes in Docker)

**Goal:** the same Step 3 behaviour (retries until attempt 8, durable configurable approval wait,
survives the app pod being deleted), running in a Kind cluster with Dapr installed in Kubernetes
mode and **Redis** as the state store.

### 8.1 Prerequisites (need a human; may need IT)

- A container runtime: **Docker Desktop** (preferred; needs admin) or an alternative Kind supports
  (e.g. Podman, `KIND_EXPERIMENTAL_PROVIDER=podman`). Check the current Kind docs.
- `kind`, `kubectl` on PATH. With winget blocked, download the Windows binaries from their GitHub
  releases into `tools\` (same pattern as `tools\dapr`) and add them to PATH in `Refresh-Path`.
- `dapr` CLI (already in `tools\dapr`).
- Enough disk (~5–10 GB for images). The laptop's C: drive may be tight.

### 8.2 Run the existing deploy script

```powershell
.\run step3-kind                                   # → step3\k8s\deploy-kind.ps1
kubectl get pods                                   # redis, publisher Running; story-agent 2/2 (app + daprd)
kubectl port-forward svc/story-agent 8000:8000     # keep running
.\run step3-verify                                 # in another terminal → ALL CHECKS PASSED
```

`deploy-kind.ps1` does: check tools → `kind create cluster --name story` → `dapr init -k --wait --timeout 600`
→ `docker build -t story-agent:dev .` → `kind load docker-image` → secret `story-agent-env` from `.env`
(parsed, CRLF-safe) → `kubectl apply` the four manifests → `rollout restart` + `rollout status`.
It was **dry-run tested with stub tools only**; it has never run against a real cluster.

### 8.3 Likely issues to watch for (not yet observed; fix if they occur)

| Risk | Symptom | Likely fix |
| --- | --- | --- |
| App starts before the Dapr sidecar is ready | App logs gRPC connection errors at startup; workflows never run | In `lifespan`, wait for the sidecar (e.g. poll `http://localhost:3500/v1.0/healthz/outbound` or use `runtime.wait_for_worker_ready()`), or add annotation `dapr.io/sidecar-listen-addresses`/startup ordering. Check current Dapr guidance |
| Dapr scheduler pods Pending | `kubectl get pods -n dapr-system` shows scheduler Pending (PVC) | Kind ships a `standard` StorageClass; check PVCs. Or install Dapr with `--set dapr_scheduler.cluster.inMemoryStorage=true` for a dev cluster (check the flag for 1.18) |
| Image not found | `ErrImageNeverPull` | `kind load docker-image story-agent:dev --name story` again; manifests use `imagePullPolicy: Never` |
| Secret missing | Pod `CreateContainerConfigError` | Re-run the secret step; check `kubectl get secret story-agent-env` |
| MCP host check | 421/400 from MCP server | `publisher.py` binds `MCP_HOST=0.0.0.0` in K8s; FastMCP's DNS-rebinding protection only applies to localhost hosts. Verified locally by hostname, not in a cluster |
| Corporate proxy | Image pulls / pip install inside `docker build` fail | Configure Docker proxy settings; add `HTTP(S)_PROXY` build args |
| Port 8000 busy | port-forward fails | `.\run stop` first (stops local Step 3) |

### 8.4 Restart test in Kind (acceptance)

```powershell
cd step3
..\.venv\Scripts\python scripts\verify_step3.py start
kubectl delete pod -l app=story-agent
kubectl get pods -w                     # wait for new story-agent pod 2/2 Running
kubectl port-forward svc/story-agent 8000:8000   # port-forward dies with the old pod: restart it
..\.venv\Scripts\python scripts\verify_step3.py resume
```

Optional improvement: add a `step3-kind-restart-test` action to `runner.ps1` that automates this
(delete pod → wait for ready → restart port-forward in a background job → resume).

### 8.5 Acceptance criteria for "Kind done"

- [ ] `kubectl get pods`: `redis`, `publisher` Running; `story-agent` `2/2 Running`.
- [ ] `verify_step3.py` (`run`) against the port-forward → **ALL CHECKS PASSED** (published on attempt 8; 10 s window expires; 410 on late approval).
- [ ] Restart test (8.4) → **ALL CHECKS PASSED**; story still `awaiting_approval` after the pod was replaced.
- [ ] Workflow state is in Redis (e.g. `kubectl exec deploy/redis -- redis-cli --scan | head` shows Dapr keys).
- [ ] `published` story visible: `kubectl exec deploy/publisher -- ls /data/published`.
- [ ] README updated with the exact commands that worked, and the pass reports saved as `verify_output_step3_kind_*.txt`.

---

## 9. Optional follow-ups (only if asked)

1. **Framework comparison write-up:** Dapr vs Temporal vs DBOS (and Dapr Agents) for this use case.
   Workflow logic would be the same; differences are setup, maturity, tooling (Temporal UI), and
   granularity (Dapr Agents makes each LLM/tool call durable; today a crash mid-`write_story` re-runs both LLM calls).
2. **Skill tightening:** GPT-4o once quoted a film line; strengthen `SKILL.md` ("never quote dialogue").
3. **Security:** the OpenAI key was partially visible in a shared screenshot. The developer should rotate it.
4. **Step READMEs** (`step1/README.md`, `step2/README.md`, `step3/README.md`) still contain some bash-style commands;
   the top-level README is the Windows source of truth.
5. **Kind-mode verify:** `verify_step3.py` already accepts `--url`; no change needed unless the port differs.

---

## 10. Rules for changes

- Keep `step1/` and `step2/` unchanged (frozen demos).
- Never commit or print `.env` / the API key. `.gitignore` already excludes `.env`, `.venv`, `dapr/state.db*`, `published/`, `verify_output_*`.
- Keep `runner.ps1` compatible with **Windows PowerShell 5.1** and the constraints in Section 7.
- Keep the REST API contract (Section 5) stable; the review page and `verify_step3.py` depend on it.
- After any change to Step 3, re-run `.\run step3-verify` and `.\run step3-restart-test` locally; both must end with **ALL CHECKS PASSED**.

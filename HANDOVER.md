# Handover: Durable Story Agent (Steps 1–3)

> **For the AI coding assistant (GitHub Copilot) picking this up.** Read this whole file
> before changing anything. It describes the assignment, what is built and verified, the
> exact environment constraints on the developer's laptop, and the verification status.
> Last updated: 31 October 2026.

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

## 2. Current status

| Item | Status | Evidence |
| --- | --- | --- |
| Step 1: deep agent + FastAPI + skill + human approval | **Done, verified** | `step1\verify_output_real.txt` (GPT-4o, all checks passed) |
| Step 2: flaky MCP tool integrated | **Done, verified** | Shown live on the laptop; automated checks (`step2\scripts\verify_step2.py`) passed in the development environment |
| Step 3: Dapr Workflow, retry and approval expiry | **Done, verified** | Latest Kind run `41216975350`; report artifact `step3-kind-reports` |
| Step 3: restart and durable state recovery | **Done, verified** | Latest Kind run includes app replacement, publisher replacement during publish, and Redis restart |
| Step 3: concurrent approve/reject decisions | **Done, verified** | Latest Kind run's race test accepts one decision, rejects the other with HTTP 409, and publishes the resulting workflow |
| Step 3: browser-driven reviewer journey | **Done, verified** | [Run 41216975350](https://github.com/Ayush-Deloitte2003/story-agent/actions/runs/41216975350), draft → reject with feedback → revised draft → approve → published |
| Kind deployment | **Done in GitHub Actions** | [Run 41216975350](https://github.com/Ayush-Deloitte2003/story-agent/actions/runs/41216975350), success on commit `3cb12a8` |
| Local Kind deployment | **Not run** | Docker/Kind are unavailable on the corporate laptop; CI is the integration environment |

The original implementation and subsequent changes are documented in
`WORK_HISTORY.md`. The latest code changes are committed on
`ayush-deloitte2003-story-agent-evidence-persistence-dashboa`; no PR was opened.
The latest code commit is `3cb12a8` (browser reviewer journey); this handover
update is the current pending documentation change. The browser report from run
`41216975350` is saved under
`step3\verify_output_step3_kind_run_41216975350\` and remains ignored by Git.
Other local `step3\verify_output_step3_*` files are also ignored evidence.

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
└── step3/                     # Current implementation of the durable workflow
```

`step1` and `step2` are frozen snapshots kept so each step can be demonstrated as it was.
Implementation changes should remain in `step3/` and `scripts/runner.ps1` unless
the user explicitly asks for a broader change. Root documentation has been
updated as part of the project-history handoff.

### step3/ in detail

| Path | Purpose |
| --- | --- |
| `app/workflow.py` | **Core.** Dapr workflow `story_workflow` + activities `write_story`, `publish_story`; retry policies |
| `app/approval.py` | Revision-specific approval event names and single-process decision claims |
| `app/agent.py` | Deep agent (`create_deep_agent`), `draft_story()` (runs the agent until it proposes `publish_story`), `publish_via_mcp()` (one MCP call), `load_mcp_tools()` |
| `app/main.py` | FastAPI app; starts the Dapr `WorkflowRuntime` in `lifespan`; REST API (Section 5) |
| `app/index.html` | Reviewer dashboard with recent workflows, refresh recovery, lifecycle timeline, expiry countdown, revision comparison, and Markdown downloads |
| `mcp_server/publisher.py` | MCP server (FastMCP from the official `mcp` SDK, streamable HTTP) exposing `publish_story`; fails the first `MCP_FAIL_TIMES` attempts **per story** |
| `skills/hp-season-1/SKILL.md` | The skill the agent loads |
| `dapr/components/statestore.yaml` | Local state store: `state.sqlite`, file `dapr/state.db`, `actorStateStore: true` |
| `scripts/verify_step3.py` | HTTP-level checks against the running app: `run`, `race`, recovery, expiry, retry-exhaustion modes; `--url` option |
| `scripts/verify_browser_review.py` | Playwright Chromium test of the actual reviewer flow, including feedback, revision comparison, approval, publication, download, and timeline |
| `requirements-browser-test.txt` | Pinned, test-only Playwright dependency, separate from runtime requirements |
| `tests/test_publisher.py` | Deterministic publisher persistence/idempotency and decision-claim tests; no API key needed |
| `requirements-kind.lock` | Exact tested transitive Python dependencies used by the Kind image |
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
| POST | `/chat/{thread_id}/decision` | `{"decision": "approve"\|"reject", "feedback"?: str, "expected_revision"?: int}` | 404 unknown; 410 if expired; 409 if not awaiting approval, stale, or another decision is being submitted. Decision is revision-scoped. Reject → waits for next revision; approve → returns once publishing started |
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

Tested integration versions: Python 3.12, Dapr CLI 1.18.2, and Dapr runtime
1.19.0. Python package versions are pinned in `step3\requirements.txt`; the
container installs the complete tested transitive set from
`step3\requirements-kind.lock` (including `mcp==1.30.0` and
`litellm==1.105.0` for compatibility).

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

## 8. Kind deployment and current verification

The Kind deployment is implemented and has been run successfully in GitHub
Actions. Local Kind is **not available on the developer laptop** because Docker
is not installed; use the workflow for full integration validation rather than
claiming a local cluster run.

### Latest successful run

- Workflow: `step3-kind`
- Run: [41216975350](https://github.com/Ayush-Deloitte2003/story-agent/actions/runs/41216975350)
- Tested code commit: `3cb12a897a82f617bcfd4b0f9ad3147591325a69`
- Conclusion: success
- Artifact: `step3-kind-reports`
- Dapr CLI: `1.18.2`; Dapr runtime: `1.19.0`; Python: `3.12`

The workflow passed deterministic tests, cluster creation and Dapr installation,
deployment, the main story flow, and these recovery/failure scenarios:

- App pod deletion while awaiting approval; the workflow remained recoverable.
- App scaled down and publisher replaced during publish retries; state/files
  survived and publishing completed on attempt 8.
- Redis restarted while awaiting approval; the workflow was recovered and
  completed.
- Approval expired while the app was unavailable; late approval returned HTTP
  410.
- Retry exhaustion ended in `failed`.
- A simultaneous approve/reject request pair accepted exactly one decision;
  the other returned HTTP 409. Rejection won in this run, created the next
  revision, and that revision published on attempt 8.
- The Playwright Chromium test drove the actual browser UI through initial
  draft review, rejection with feedback, revised draft comparison, approval,
  publication, and download. The downloaded Markdown matched the approved
  revision, and the timeline recorded the review lifecycle.
- Redis workflow keys and published Markdown were checked against the approved
  draft.

To dispatch another run from PowerShell with `gh` installed and authenticated:

```powershell
gh workflow run step3-kind.yml --repo Ayush-Deloitte2003/story-agent --ref ayush-deloitte2003-story-agent-evidence-persistence-dashboa
gh run list --repo Ayush-Deloitte2003/story-agent --workflow step3-kind.yml --branch ayush-deloitte2003-story-agent-evidence-persistence-dashboa --limit 1
gh run watch <run-id> --repo Ayush-Deloitte2003/story-agent --exit-status
gh run download <run-id> --repo Ayush-Deloitte2003/story-agent --name step3-kind-reports --dir <evidence-folder>
```

The repository Actions secret `OPENAI_API_KEY` is injected only by the workflow;
never print or commit it. The downloaded reports include
`verify_output_step3_kind_*.txt`, Redis/pod evidence, publisher state, approved
drafts, and published Markdown. The browser report passed all checks and is
saved locally at
`step3\verify_output_step3_kind_run_41216975350\verify_output_step3_kind_browser.txt`.
Existing local evidence is ignored by Git.

### Decision serialization scope

The decision endpoint serializes submissions within the current single FastAPI
process. The race test verifies one accepted decision per revision in this
single-replica deployment. This is **not a distributed lock**; if the API is
scaled to multiple replicas, use a shared atomic coordination mechanism and
retest the race across replicas.

---

## 9. Remaining work and optional follow-ups

The originally requested work and the follow-up race test are complete and
verified; no blocking implementation task remains in this scope. Possible
future work, only if requested:

1. **Multi-replica decision coordination:** replace the in-process lock with a
   shared atomic claim if the FastAPI deployment needs multiple replicas.
2. **Shared workflow history:** the dashboard currently stores recent workflow
   snapshots and its timeline in browser local storage. A server-side,
   multi-user history/audit feature would require a separately scoped design.
3. **Local Kind:** optionally validate on a local Docker/Kind environment if
   one becomes available. The current successful validation is in GitHub
   Actions.
4. **Framework comparison or skill changes:** Dapr vs Temporal/DBOS, or
   additional writing-skill constraints, are unrelated optional extensions.

---

## 10. Rules for changes

- Keep `step1/` and `step2/` unchanged (frozen demos).
- Never commit or print `.env` / the API key. `.gitignore` already excludes `.env`, `.venv`, `dapr/state.db*`, `published/`, `verify_output_*`.
- Keep `runner.ps1` compatible with **Windows PowerShell 5.1** and the constraints in Section 7.
- Keep the REST API contract (Section 5) stable; the review page and `verify_step3.py` depend on it.
- After Step 3 changes, run deterministic tests locally and dispatch `step3-kind`
  for integration validation when local Docker/Kind is unavailable. Do not
  claim local Kind verification unless it was actually run locally.

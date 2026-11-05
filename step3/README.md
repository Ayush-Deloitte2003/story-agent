> **On Windows, use the commands in the README one folder up (`.\run ...`).** This file explains the code.

# Story agent (Step 3: durable with Dapr)

A deep agent behind FastAPI writes a story, a human approves it, and a flaky
MCP tool publishes it. **Dapr Workflow** makes the whole thing durable:

```
POST /chat ──> Dapr workflow "story_workflow"
                 1. activity write_story ── deep agent reads SKILL.md, LLM writes the story,
                                            proposes publish_story(title, story)
                 2. durable wait for the revision-specific approval event   ⏳ (default 24h)
                      reject + feedback ──> back to 1 with the feedback
                      timer fires first ──> ends as "expired"
                 3. activity publish_story ── MCP tool on the publisher server
                      RuntimeError ──> Dapr retries with backoff (1s, 1.5s, 2.3s … up to 10s)
                      attempt 8 succeeds ──> ends as "published"
POST /chat/{thread_id}/decision ──> raises the revision-specific approval event
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

The publisher stores its attempt ledger and Markdown output on a Kubernetes
PersistentVolumeClaim. The workflow passes its stable workflow ID as the MCP
idempotency key. The publisher stores a content fingerprint with the result,
returns the saved result for a retry with matching content, and rejects reuse
of that key for different content. Attempts and published-result records are
atomically replaced. This protects the publisher's filesystem side effects
from a lost response; it is not a claim of end-to-end exactly-once delivery.

Approval events are revision-specific. The existing decision endpoint also
accepts an optional `expected_revision`; stale browser decisions receive HTTP
409, while existing clients that omit the field retain the original behavior.

### Event-triggered workflow intake

The Kind deployment also subscribes to Dapr Pub/Sub topic `story-events`. A
CloudEvent with type `story.requested` is routed to the existing
`story_workflow`; other event types go to a drop route. The event `data` must
contain a non-empty `message`, with an optional positive
`approval_timeout_seconds`. The workflow still drafts first and waits for the
same human approval before publication.

The workflow ID is derived deterministically from the CloudEvent `source` and
`id`. Dapr Pub/Sub delivers at least once, so a redelivered event maps to the
same workflow ID instead of intentionally starting a second workflow. If
scheduling fails before the workflow is visible, the subscriber returns
`RETRY`; invalid story events return `DROP`. The `POST /webhooks/story-requested`
gateway accepts an event ID, source, and message, then publishes a CloudEvent
through the local Dapr sidecar. Callers must reuse the same ID when retrying
their webhook request. Query an event-triggered workflow with
`GET /events/{event_id}?source={source}`. This is event-to-workflow
deduplication, not general multi-replica coordination or an exactly-once
guarantee.

The subscription has a bounded exponential delivery retry policy and routes
messages that still cannot be delivered to the `story-events-dead` topic.
There is deliberately no consumer for that topic yet, so dead-lettered events
remain for operator inspection rather than being silently acknowledged; the
Agent Inbox phase can add a durable review/notification consumer.

The same topic also routes `story.quality-review.requested` to an independent
`story_quality_review_workflow`. Its event data contains a `title`, `story`, and
optional original `request`. The workflow runs a story-quality reviewer and
returns a validated score, recommendation, strengths, issues, and suggestions.
Submit one through `POST /webhooks/story-quality-review-requested` and check its
status at `GET /events/quality-review/{event_id}?source={source}`. Reuse the
same event ID on retries; its deterministic workflow identity is separate from
the writing workflow. A `ready` recommendation is advisory only and never
replaces human approval or triggers publishing. The review activity is
side-effect-free; Dapr retries transient failures up to three total attempts.
The Kind workflow also replaces the app pod after scheduling a review, replays
the same event ID, and verifies that the durable workflow returns its completed
review under the original workflow ID.
The event integration check also completes that review while an independent
story workflow remains paused awaiting human approval. This demonstrates
concurrent workflow lifecycles in the current single-replica deployment; it is
not a load or multi-replica scaling benchmark.

The manual Kind workflow includes a 10-workflow quality-review concurrency
baseline. It submits ten independent review events together, samples their
nonterminal workflow count while polling, and reports completion rate, elapsed
time, mean/p50/p95 completion latency, and observed completion throughput in
`verify_output_step3_kind_load.txt`. This is a small baseline on one app
replica, not proof of production capacity or a CPU/memory benchmark. It runs
real model calls and may encounter provider rate limits; do not interpret a
failed run as evidence that workflow state was lost.
The first baseline passed in [Actions run 41995787239](https://github.com/Ayush-Deloitte2003/story-agent/actions/runs/41995787239):
10/10 events completed; ten active workflows were observed in a poll sample;
total elapsed time was 4.70s, mean latency 4.28s, p50 4.69s, and p95 4.70s.
The short-burst throughput calculation was 127.69 completed reviews/minute;
this is not a sustainable rate or production capacity claim. This run did not
collect CPU/memory, token/429, broker-lag, or multi-replica metrics.

For example, submit a webhook to the gateway:

```json
{
  "id": "booking-123",
  "source": "booking-service",
  "message": "Write a story about a lighthouse.",
  "approval_timeout_seconds": 86400
}
```

The gateway wraps this in a `story.requested` CloudEvent and publishes it
through its local Dapr sidecar. A successful response means Dapr accepted the
event; it does not mean the story has already been drafted.

To request an independent quality review, submit the story and a new event
identity to `POST /webhooks/story-quality-review-requested`:

```json
{
  "id": "review-123",
  "source": "story-service",
  "title": "The Lantern",
  "story": "Mina found a lantern and returned it to the neighbor who had lost it.",
  "request": "Write a warm story about helping a neighbor."
}
```

Poll `GET /events/quality-review/review-123?source=story-service` for the
durable review result. This separate event does not change any story workflow
or publish content.

In the current Kind setup, Dapr Pub/Sub and workflow state use the same Redis
service for a small demonstration. Production scale should use an appropriately
sized broker and state store, with independent capacity, monitoring, and failure
policies. The `k8s/resiliency.yaml` policy governs only transient Pub/Sub
delivery; it does not replace activity retry settings in `app/workflow.py`.
Local slim-mode setup does not configure a broker; event intake is currently
enabled and integration-tested in Kind.

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
python -m unittest discover -s tests -v         # deterministic, no API key or Dapr needed
.venv\Scripts\python scripts\verify_step3.py          # revise, retries, expiry
.venv\Scripts\python scripts\verify_step3.py start    # leave a story awaiting approval
#   ...stop and restart the app (Ctrl+C + dapr run again, or delete the pod in Kind)...
.venv\Scripts\python scripts\verify_step3.py resume   # it is still there; approve and publish
```

Local reports are saved as `verify_output_step3_<mode>.txt`; the Kind workflow
uses the `verify_output_step3_kind_<mode>.txt` prefix.

The browser stores up to ten recent workflow snapshots in local storage, refreshes
the selected workflow from the server on reload, and shows its current status,
approval countdown, workflow progress, browser-observed timeline, and
previous-draft comparison. Example request buttons, a live character count, and
Ctrl+Enter make starting a story quicker. During review, the page clearly
separates the feedback-for-revision action from approving and publishing. Use
**Download published Markdown** to save the approved or published draft.
Timeline entries are client-observed and therefore are not a durable server audit
log. Removing an item clears only this browser's saved copy; it does not cancel
the server-side workflow.

The manual `step3-kind` GitHub Actions workflow uses Python 3.12, Dapr CLI
1.18.2, and Dapr runtime 1.19.0. `requirements.txt` pins the direct Python
dependencies to versions resolved by the successful Kind run; Docker installs
the complete transitive set from `requirements-kind.lock`, captured from that
same run. The workflow tests publisher persistence/idempotency and stale
revision decisions, races simultaneous approve/reject requests and requires
exactly one to be accepted, checks the exact successful attempt number and Redis
workflow key prefix, compares the downloaded published Markdown byte-for-byte
with the approved draft, and then restarts the publisher to check its volume.
It also runs a Chromium browser journey through the actual reviewer page:
create a draft, reject with feedback, inspect the revision comparison, approve
the new draft, wait for publication, and verify the downloaded Markdown and
browser-observed timeline.
It also tests app-pod replacement, quality-review replay after app replacement,
Redis restart recovery, expiry while the app is down, and exhausted publish
retries. The GitHub job summary and uploaded `step3-kind-reports` artifact
include verification reports and the published Markdown.

Decision submission is serialized within the single FastAPI process used by
the current local and Kind deployments. This prevents two simultaneous
requests to that process from raising conflicting decisions for the same
revision. It is not a distributed lock for a future multi-replica API
deployment.

Only the earlier Kind execution at commit `3754167` is a completed cluster
verification. Changes in progress must be run through `step3-kind` before they
can be described as verified on Kind. Local unit tests do not require the
OpenAI key; the Kind workflow does and receives it through the repository
Actions secret.

## Layout

| File | What it does |
| --- | --- |
| `app/workflow.py` | The Dapr workflow, its two activities and the retry policies |
| `app/agent.py` | Deep agent (skill + LLM) that drafts the story; MCP client for publishing |
| `app/main.py` | Webhook gateway, Pub/Sub subscriber, and FastAPI workflow endpoints |
| `app/events.py` | CloudEvent validation and stable event-to-workflow IDs |
| `app/quality_review.py` | Strict validation for the specialist review result |
| `app/index.html` | Reviewer dashboard: recent workflows, timeline, countdown, revision comparison, downloads |
| `mcp_server/publisher.py` | Flaky MCP server: each story fails 7 times, then publishes |
| `skills/hp-season-1/SKILL.md` | The skill the agent follows to write the story |
| `dapr/components/statestore.yaml` | Local state store (SQLite) for `dapr run` |
| `k8s/pubsub.yaml` | Redis-backed Dapr Pub/Sub component for event-triggered workflows |
| `k8s/resiliency.yaml` | Bounded Pub/Sub delivery retries before dead-lettering |
| `k8s/` | Kind manifests and `deploy-kind.ps1` |
| `Dockerfile` | One image for the app and the publisher |
| `scripts/verify_step3.py` | PASS/FAIL checks against the running app |
| `scripts/verify_event_intake.py` | Kind check for story and quality-review event delivery, deduplication, concurrent workflow lifecycles, approval, and publication |
| `scripts/verify_quality_review_recovery.py` | Kind check for quality-review workflow recovery and same-ID event replay after app replacement |
| `scripts/verify_quality_review_load.py` | Small, manual Kind baseline for ten concurrent quality-review workflows |

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

# Story Agent: Project History and Verification Record

This document brings together the original three-step assignment, the evolution
of the implementation, the latest Step 3 improvements, and the verification
evidence available on 31 October 2026. It supplements the root `README.md`,
`HANDOVER.md`, and the READMEs in `step1/`, `step2/`, and `step3/`; it does not
replace those run guides.

## Project goal

The project demonstrates a story-writing agent whose work is reviewed by a
human before publication. It was built in three increments:

1. A FastAPI application runs a skill-guided deep agent, returns a draft, and
   waits for a human to approve it or reject it with feedback.
2. Publication is moved behind a separate MCP server whose publishing tool
   deliberately fails for a configured number of attempts.
3. Dapr Workflow provides durable approval waits and retries the failed
   publishing activity. Kubernetes/Kind adds a Redis-backed state store and
   provides a place to exercise recovery across component restarts.

Human approval remains a required gate before publication throughout the
workflow.

## Original three-step implementation

### Step 1: draft and human approval

Step 1 established the application flow and the user-facing interface:

- FastAPI serves the review page and exposes the `/chat` API.
- A deep agent uses the configured writing skill and language model to create
  a draft and propose publication.
- The reviewer can approve the draft or reject it with feedback for another
  revision.
- Approval is time-limited (24 hours by default).
- Approved content is saved as Markdown.

At this stage, workflow and approval state lived in application memory.
Restarting the process lost in-progress work; this was an intentional baseline
for the durability improvements in Step 3.

### Step 2: publication through a flaky MCP server

Step 2 introduced a separate MCP publisher with a `publish_story` tool:

- The publisher is configured to fail the first seven attempts for a story
  with a runtime error, then succeed on attempt eight.
- The Step 2 app calls the MCP tool after human approval.
- There is no durable retry mechanism in this step: the first publication
  error fails the run, and the approval cannot simply be reused.
- The publisher stores attempt data independently of the app.

This step demonstrates why a durable workflow engine is useful: the
application-level approval can succeed while the downstream publishing call
still fails.

### Step 3: durable workflow with Dapr

Step 3 moves orchestration to Dapr Workflow:

1. A workflow activity asks the agent to create a draft.
2. The workflow records its approval state and waits for either a
   revision-specific human decision or an approval timer.
3. Rejection starts another drafting iteration with feedback.
4. Approval starts a publishing activity. Dapr retries failures using
   configurable intervals and backoff.
5. The workflow ends in `published`, `expired`, or `failed`, as appropriate.

The REST API remains centered on:

- `GET /` — serves the reviewer page.
- `POST /chat` — starts a workflow.
- `GET /chat/{thread_id}` — reads current workflow state.
- `POST /chat/{thread_id}/decision` — approves or rejects the current draft.

Dapr persists workflow state in its configured state store. Local execution
uses SQLite; the Kind deployment uses Redis. The approval wait is represented
as a durable timer, so it continues to elapse while the app process is down.

## Improvements added in the latest work

The work requested after the original three steps was completed in order:
reproducible evidence, publisher durability/idempotency and recovery
verification, then reviewer dashboard improvements.

### Reproducible dependencies and stronger evidence

- Direct Python requirements are pinned, and
  `step3/requirements-kind.lock` records the tested transitive dependency set.
- The Kind workflow pins the Dapr CLI to `1.18.2` and targets Dapr runtime
  `1.19.0`.
- Verification now requires successful publishing on exactly attempt 8.
- The workflow checks that expected Dapr workflow keys are present in Redis.
- It exports published Markdown and compares it with the approved draft.
- Reports, publisher state snapshots, approved-draft JSON, and published
  Markdown are uploaded as the `step3-kind-reports` artifact.
- The workflow writes a job summary with runtime details, report checks, and
  artifact reference.
- `step3/README.md` distinguishes deterministic local tests from live
  model-backed Kind verification.

### Durable publisher state and retry safety

- The publisher deployment uses a PersistentVolumeClaim for its attempt ledger
  and published Markdown instead of ephemeral `emptyDir` storage.
- The publisher serializes updates and atomically replaces its saved state.
- Published filenames are derived from story content, providing a stable
  identity across retries.
- A retry after a successful publish returns the stored result rather than
  writing another publication file.
- The deployment uses a single replica with `Recreate` strategy, consistent
  with its ReadWriteOnce storage.
- Tests cover state surviving module/process reload, retry idempotency,
  concurrent calls, failed attempts, and exact Markdown content.

These measures protect the publisher's filesystem effects across the tested
failure/retry cases. They are **not** an end-to-end exactly-once guarantee:
external side effects and lost responses cannot generally be made exactly once
without cooperation from every participant.

### Revision-aware approval decisions

- Approval events are scoped to a draft revision.
- The decision endpoint accepts the additive optional field
  `expected_revision`.
- A stale browser decision with a mismatched revision is rejected with HTTP
  409. Clients that omit the optional field retain the established request
  shape.
- Verification exercises rejection/revision changes and refusal of a delayed
  decision for the previous revision.

A truly simultaneous approve-versus-reject race was **not separately tested
end-to-end** at the time of the initial durability work. A follow-up added
single-process decision serialization and a Kind race test; run `41212381229`
passed with the rejection accepted, the competing approval rejected with HTTP
409, a new revision created, and that revision subsequently published on attempt
8. The serialization is process-local and assumes the current one-replica API
deployment; it is not a distributed lock for a future multi-replica service.

### Reviewer dashboard

The existing Step 3 page was enhanced without introducing a multi-user or
server-side history service:

- The browser stores up to ten recent workflow snapshots in local storage.
- A selected workflow can be refreshed from the server after a browser reload.
- The page displays current status and a browser-observed lifecycle timeline.
- It displays an approval countdown and disables approval after expiry.
- It compares the current draft with a previous revision.
- It offers Markdown download for approved or published content.
- Polling and controls are guarded to avoid stale responses or invalid actions
  when changing workflows.

The dashboard timeline and recent-workflow list are **browser-local**, not a
durable server audit log. Removing an entry clears the browser's saved
reference; it does not cancel the underlying workflow.

## Compatibility and reproducibility notes

The tested container dependencies include these important compatibility
constraints:

- `mcp==1.30.0` is pinned to the FastMCP API used by this project. MCP 2.x
  removes the relevant compatibility surface.
- `litellm==1.105.0` is pinned because the following release line required
  MCP 2.x, conflicting with this project's MCP 1.x dependency.
- The lockfile contains the full tested transitive dependency set rather than
  relying on unconstrained latest releases during image builds.
- GitHub Actions uses Python 3.12, Dapr CLI 1.18.2, and Dapr runtime 1.19.0
  for the Kind integration run.

An earlier compatibility fix replaced a `head`-based pipeline in the
workflow-summary path because `head` could close the pipe early and trigger a
SIGPIPE under `pipefail`.

## Verification record

### Deterministic local checks

The no-key Step 3 test suite contains five tests covering:

- revision-specific approval event names;
- concurrent publisher retries producing one publication;
- a failed publish not producing a story file;
- publisher state surviving restart/reload and successful retry remaining
  idempotent;
- exact comparison of published Markdown with the approved draft.

The suite passed locally on 31 October 2026. Python compilation of the
verification scripts also passed. YAML/Bash syntax checks and browser smoke
checks were previously completed. The local laptop/worktree did not have
Docker, so local Kind execution was not available.

### GitHub Actions Kind integration

The full `step3-kind` workflow was run successfully more than once. The latest
run is:

- Run: [41212381229](https://github.com/Ayush-Deloitte2003/story-agent/actions/runs/41212381229)
- Commit tested: `e7d316866206fbf266e190e6e604e25a085eb574`
- Conclusion: **success**
- Uploaded artifact: `step3-kind-reports`

The workflow's successful steps included:

- deterministic publisher/workflow tests;
- Kind cluster creation and Dapr installation;
- image build, deployment, and secret injection through the repository's
  configured Actions secret;
- main story drafting, rejection/revision, approval, retries, and publishing;
- delayed decision for a previous revision rejected with HTTP 409;
- published story successful on exactly attempt 8;
- app pod deletion and recovery of an awaiting workflow;
- app and publisher replacement while publishing was in progress, with
  publishing resuming and completing on attempt 8;
- Redis restart while a workflow awaited approval, followed by recovery and
  publication;
- approval expiring while the app was unavailable, with late approval refused
  using HTTP 410;
- retry exhaustion ending in `failed`;
- simultaneous approve/reject submission accepting only one decision, then
  creating a new draft when rejection wins and successfully publishing that
  revision on attempt 8;
- Redis workflow-key and published-Markdown evidence checks;
- report and story artifact upload.

The race report records that rejection won; the concurrent approval received
HTTP 409 with "A decision for this draft is already being processed." The
newly created revision was then published successfully.

The earlier successful run at commit `2086d04` was
[41193456896](https://github.com/Ayush-Deloitte2003/story-agent/actions/runs/41193456896).
An initial run, `41192869397`, exposed a verifier bug: the publishing-resume
mode looked for its own approved-draft evidence file rather than the file
written by the publishing-start mode. That was fixed in commit `2086d04` and
the subsequent runs passed.

### Saved evidence

The Actions artifact contains report files named
`verify_output_step3_kind_*.txt`, Redis and pod snapshots, publisher state
JSON, approved-draft JSON, and published Markdown. After run
`41193456896`, these files were downloaded into ignored paths under
`step3/`, including:

- `step3/verify_output_step3_kind_run.txt`
- `step3/verify_output_step3_kind_resume.txt`
- `step3/verify_output_step3_kind_publishing-start.txt`
- `step3/verify_output_step3_kind_publishing-resume.txt`
- `step3/verify_output_step3_kind_redis-start.txt`
- `step3/verify_output_step3_kind_redis-resume.txt`
- `step3/verify_output_step3_kind_expiry-start.txt`
- `step3/verify_output_step3_kind_expiry-resume.txt`
- `step3/verify_output_step3_kind_exhaust.txt`
- `step3/verify_output_step3_kind_published.md`
- `step3/verify_output_step3_kind_publishing-recovery.md`
- `step3/verify_output_step3_kind_publisher_before.json`
- `step3/verify_output_step3_kind_publisher_after.json`
- `step3/verify_output_step3_kind_publisher_final.json`

Additional ignored approved-draft JSON files accompany the reports. These are
local evidence files, excluded from Git; they are not part of the commits.
Run `41209780806` also uploaded its own `step3-kind-reports` artifact; run
`41212381229` is the latest full verification after adding the race test.

## Commit and branch record

The latest implementation is on branch
`ayush-deloitte2003-story-agent-evidence-persistence-dashboa`:

| Commit | Description |
| --- | --- |
| `c78c68d` | Pin MCP below version 2 for FastMCP compatibility. |
| `cfa6415` | Pin LiteLLM to the compatible tested release. |
| `3754167` | Avoid a SIGPIPE caused by `head` in the workflow. |
| `c55f71d` | Document the prior successful Kind run and dependency pins in the root README. |
| `bfdb7f8` | Add dependency reproducibility, durable publishing, recovery tests, and dashboard improvements. |
| `2086d04` | Fix the publishing-recovery verifier's approved-draft evidence lookup. |
| `e7d3168` | Serialize approval submissions and test the approve/reject race in Kind. |

The last observed tracked worktree was clean. The verification artifacts under
`step3/verify_output_step3_*` are ignored local files. No pull request was
opened.

## Scope and remaining limitations

- `step1/` and `step2/` remain historical snapshots; the recent implementation
  work targeted Step 3 and its Kind workflow.
- The full integration run was performed in GitHub Actions, not on the
  developer's laptop, because Docker/Kind was unavailable locally.
- GitHub Actions needs the repository's configured `OPENAI_API_KEY` secret to
  execute the live-model integration workflow. The key itself is not recorded
  in this document.
- Publisher idempotency is scoped to the publisher's persisted records and
  files; no global exactly-once promise is made.
- Dashboard history and timeline are local to the browser.
- Approval submission serialization is process-local; a multi-replica API would
  need a distributed coordination mechanism and additional testing.
- `HANDOVER.md` predates the latest durable-publisher/dashboard work and
  contains stale status statements about Kind verification. Use this document,
  the current `step3/README.md`, and the linked latest Actions run for the
  current implementation and validation status.

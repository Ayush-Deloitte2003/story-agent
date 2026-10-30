# Story agent: run and demonstrate Steps 1, 2 and 3

Every command below is typed in the **VS Code terminal (PowerShell)**, opened in this
folder (the one that contains `run.cmd`). Type them exactly as shown.

## 1. Setup (once, about 5 minutes)

```powershell
.\run setup
```

It installs Python (if missing), all packages, and the Dapr CLI and runtime, then asks
for your **OpenAI API key** once. Paste it and press Enter (it stays hidden). It creates
the `.env` file with your key in `step1`, `step2` and `step3`.

It ends with **"Setup complete"**. If it says to close and reopen VS Code, do that and
run `.\run setup` again.

## 2. Step 1: deep agent, story, human approval

```powershell
.\run step1-verify
```

Expect: a story printed, then a revised story, and **ALL CHECKS PASSED** (13 checks).

```powershell
.\run step1-demo
```

The browser opens. Click **Write story**, type feedback and click **Reject and revise**,
then **Approve and publish**. The story is saved in `step1\published`.

## 3. Step 2: MCP tool that throws an exception

```powershell
.\run step2-verify
```

Expect: **ALL CHECKS PASSED** (18 checks), including "After approval, the MCP tool's
RuntimeError stops the run".

```powershell
.\run step2-demo
```

Two windows open (MCP server, app) and the browser. Click **Write story**, then
**Approve and publish**. The page says **Publishing failed after approval**, and the
"Step 2 MCP server" window shows `attempt 1 of 8: FAIL`. Nothing retries.

## 4. Step 3: Dapr retries and durable approval

```powershell
.\run step3-demo
```

Windows open for Dapr placement, Dapr scheduler, the MCP server and the app, then the
browser. Click **Write story**, then **Approve and publish**. In the "Step 3 MCP server"
window, attempts 1 to 7 fail with growing gaps and attempt 8 publishes; after about
40 seconds the page says **Published**.

With Step 3 still running:

```powershell
.\run step3-verify
```

Expect: **ALL CHECKS PASSED** (12 checks): revision, publish on attempt 8, and a
10-second approval window that expires.

```powershell
.\run step3-restart-test
```

This leaves a story waiting for approval, **stops the app**, starts it again, then
approves the same story. Expect: "After the restart, the story is still awaiting
approval" and **ALL CHECKS PASSED**.

## 5. Step 3 on a Kind cluster (optional, needs Docker)

```powershell
.\run kind-tools
```

Restart Windows, open **Docker Desktop** and wait until it says it is running. Then:

```powershell
.\run step3-kind
kubectl port-forward svc/story-agent 8000:8000
```

Leave the port-forward running, open a second terminal in this folder and run
`.\run step3-verify`.

## Stop everything

```powershell
.\run stop
```

Run this before switching from one step's demo to another if a window is still open
(the demo commands also stop the previous step automatically).

## If something goes wrong

| Message | Fix |
| --- | --- |
| `.\run` is not recognised | Make sure the terminal is in this folder: `cd` into the folder that contains `run.cmd` |
| "Run '.\run setup' first." | Run `.\run setup` |
| Authentication error from OpenAI | Delete the `.env` file in this folder and run `.\run setup` again to re-enter the key |
| A window shows an error | Send a screenshot of that window |

The folders `step1`, `step2` and `step3` hold the project as it was at the end of
each step; their own READMEs explain the code.

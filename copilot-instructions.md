# Copilot instructions

Before making changes, read `HANDOVER.md` at the repository root. It explains the assignment,
what is already built and verified, the Windows/corporate-laptop constraints, and the remaining
work (running Step 3 on a Kind cluster, Section 8).

Key rules:
- Work only in `step3/` and `scripts/runner.ps1`; `step1/` and `step2/` are frozen snapshots.
- Target Windows PowerShell 5.1 (no `winget`, no admin, port 8080 is taken).
- Never read out, print or commit `.env` or the OpenAI API key.
- Keep the REST API in `step3/app/main.py` stable; `step3/scripts/verify_step3.py` and `app/index.html` depend on it.
- After changes, `.\run step3-verify` and `.\run step3-restart-test` must both end with ALL CHECKS PASSED.

"""Exercise the reviewer journey in a real browser against the running app."""

import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.getenv("STORY_AGENT_URL", "http://127.0.0.1:8000")
REPORT_PREFIX = os.getenv("VERIFY_REPORT_PREFIX")
REPORT_NAME = (
    f"verify_output_step3_{REPORT_PREFIX}_browser.txt"
    if REPORT_PREFIX
    else "verify_output_step3_browser.txt"
)

lines: list[str] = []
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += 0 if ok else 1
    line = f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else "")
    lines.append(line)
    print(line, flush=True)
    if not ok:
        raise AssertionError(line)


def verify_review_journey() -> None:
    print(f"Browser reviewer journey against {BASE_URL}", flush=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(accept_downloads=True)
            page.set_default_timeout(120_000)
            decisions: list[dict] = []

            page.locator("#message").fill("A temporary prompt to check the example buttons.")
            page.get_by_role("button", name="Finding friendship").click()
            example_text = page.locator("#message").input_value()
            check("Example prompt updates the request and character count",
                  "new student finding a friend" in example_text
                  and page.locator("#request-count").inner_text() == f"{len(example_text)} / 2000")

            def record_decision(request) -> None:
                if request.method == "POST" and request.url.endswith("/decision"):
                    decisions.append(json.loads(request.post_data or "{}"))

            page.on("request", record_decision)
            page.goto(BASE_URL, wait_until="networkidle")
            page.locator("#message").fill(
                "Write a brief season 1 story about a new student. Keep it concise."
            )
            page.get_by_role("button", name="Write story").click()
            page.locator("#status").wait_for()
            page.wait_for_function(
                "() => document.querySelector('#status').textContent.includes('awaiting_approval')"
            )

            first_story = page.locator("#story").inner_text()
            check("A draft is displayed and waiting for human review",
                  bool(first_story.strip()) and "draft 1" in page.locator("#status").inner_text(),
                  page.locator("#status").inner_text())
            check("Workflow progress highlights the human review step",
                  page.locator("#progress-draft").get_attribute("class") == "complete"
                  and page.locator("#progress-review").get_attribute("class") == "active",
                  "draft complete; human review active")
            check("Review actions are available before publication",
                  page.locator("#review-controls").is_visible()
                  and page.get_by_role("button", name="Approve and publish").is_enabled()
                  and not page.locator("#download").is_visible(),
                  "approve/reject visible; published-content download hidden")

            feedback = "Make the story more encouraging and emphasize the new student's friendship."
            page.locator("#feedback").fill(feedback)
            page.get_by_role("button", name="Reject and revise").click()
            page.wait_for_function(
                "() => { const s = document.querySelector('#status').textContent; "
                "return s.includes('awaiting_approval') && s.includes('draft 2'); }"
            )
            revised_story = page.locator("#story").inner_text()
            check("Reject with feedback returns a revised draft",
                  bool(revised_story.strip()) and revised_story != first_story,
                  f"draft 1={len(first_story)} chars, draft 2={len(revised_story)} chars")
            check("The browser submits the reviewer's rejection feedback",
                  decisions[-1].get("decision") == "reject"
                  and decisions[-1].get("feedback") == feedback,
                  json.dumps(decisions[-1]))
            page.locator("#comparison summary").click()
            check("The dashboard compares the prior and revised drafts",
                  page.locator("#comparison").is_visible()
                  and page.locator("#previous-story").inner_text() == first_story
                  and page.locator("#current-story").inner_text() == revised_story,
                  "comparison contains both revisions")

            page.get_by_role("button", name="Approve and publish").click()
            page.wait_for_function(
                "() => document.querySelector('#status').textContent.includes('published')",
                timeout=300_000,
            )
            final_status = page.locator("#status").inner_text()
            final_story = page.locator("#story").inner_text()
            check("Approval through the browser completes publication",
                  "published" in final_status and final_story == revised_story,
                  final_status)
            check("Workflow progress marks all steps complete after publication",
                  all("complete" in (page.locator(selector).get_attribute("class") or "")
                      for selector in ("#progress-draft", "#progress-review", "#progress-publish")))

            download_button = page.get_by_role("button", name="Download Markdown")
            check("Published story download is available", download_button.is_visible())
            with page.expect_download() as download_info:
                download_button.click()
            download = download_info.value
            markdown = Path(download.path()).read_text(encoding="utf-8")
            expected_markdown = f"# {page.locator('#title').inner_text()}\n\n{final_story}\n"
            check("Downloaded Markdown matches the approved revision",
                  markdown == expected_markdown,
                  f"{download.suggested_filename}, {len(markdown)} chars")

            timeline = page.locator("#timeline").inner_text()
            check("Timeline records reject, revised draft, approval, and publication",
                  all(re.search(pattern, timeline, re.IGNORECASE) for pattern in (
                      r"Draft 1 is ready",
                      r"Draft 2 is ready",
                      r"Rejected draft 1",
                      r"Approved draft 2",
                      r"Status changed to published",
                  )),
                  timeline.replace("\n", " | "))
        finally:
            browser.close()


try:
    verify_review_journey()
except Exception as error:
    if failures == 0:
        failures = 1
        lines.append(f"[FAIL] Browser reviewer journey raised {type(error).__name__}: {error}")
    raise
finally:
    lines.append("" if failures else "ALL CHECKS PASSED")
    (ROOT / REPORT_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Browser report saved to {ROOT / REPORT_NAME}", flush=True)

if failures:
    sys.exit(1)

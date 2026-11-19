"""Drive the Step 4 reviewer UI in headless Chromium and save screenshots.

Mock advisors and simulated data only. Every state asserted is appended to
assertions.txt next to the screenshots.
"""
import json
import os
import re
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.getenv("UI_BASE", "http://127.0.0.1:8000")
SUPPORT = os.getenv("SUPPORT_SERVICE_URL", "http://127.0.0.1:8001")
OUT = Path(os.getenv("SHOTS_DIR", "shots"))
LONG = 120_000
FORBIDDEN = ("openai", "sk-", "password", "api_key", "secret")
asserted: list[str] = []
dialogs: list[str] = []
count = 0


def record(message: str) -> None:
    asserted.append(message)
    print("[ASSERT]", message, flush=True)


def shot(page, name: str) -> None:
    global count
    count += 1
    page.screenshot(path=str(OUT / f"{count:02d}-{name}.png"), full_page=True)
    text = page.inner_text("body").lower()
    for word in FORBIDDEN:
        assert word not in text, f"forbidden text {word!r} visible in {name}"


def pending_item(page, needle: str):
    page.wait_for_function(
        "needle => [...document.querySelectorAll('#inbox .item')].some(i => i.innerText.includes(needle))",
        arg=needle, timeout=LONG)
    return page.locator("#inbox .item", has_text=needle).first


def wait_status(page, status: str) -> None:
    page.wait_for_function("s => document.getElementById('b-status').innerText.includes(s)", arg=status,
                           timeout=LONG)


def book(page, seat: str) -> None:
    page.click("#b-new-key")
    page.select_option("#b-flight", "SA101")
    page.fill("#b-seat", seat)
    page.click("#b-submit")
    page.wait_for_function("document.getElementById('b-msg').innerText.startsWith('Accepted (scheduled)')",
                           timeout=LONG)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
        page.goto(BASE + "/ui")
        page.wait_for_function("document.querySelectorAll('#b-flight option').length >= 3", timeout=LONG)
        banner = page.inner_text(".banner")
        assert "Simulated mode" in banner and "in this browser only" in banner
        record("UI loads, shows the simulated-mode / browser-only banner and lists the mock flights")
        shot(page, "ui-loaded")

        book(page, "1A")
        record("Booking submitted: UI shows 'Accepted (scheduled)' (HTTP 202 contract)")
        wait_status(page, "awaiting_approval")
        item = pending_item(page, "Fare advice")
        text = item.inner_text()
        assert "Fare advice" in text and "SA101" in text and re.search(r"\d+m \d\ds", text), text
        record("Booking status 'awaiting_approval'; inbox shows fare advice (advisory only) and an expiry countdown")
        shot(page, "booking-awaiting-approval")
        item.locator("button", has_text="Approve").click()
        wait_status(page, "confirmed")
        record("After a human clicks Approve the booking becomes 'confirmed'")
        shot(page, "booking-confirmed")

        page.click("#c-submit")
        page.wait_for_function("document.getElementById('c-msg').innerText.startsWith('Accepted')", timeout=LONG)
        item = pending_item(page, "Refund (cents)")
        text = item.inner_text()
        assert "Fare rule" in text and "Request hash" in text, text
        assert int(re.search(r"Refund \(cents\): (\d+)", text).group(1)) > 0, text
        record("Cancellation accepted; inbox shows refund amount, fare rule and request hash awaiting approval")
        wait_status(page, "confirmed")
        record("Booking is still 'confirmed' until the cancellation is approved")
        shot(page, "cancellation-awaiting-approval")
        item.locator("button", has_text="Approve").click()
        wait_status(page, "cancelled")
        record("After approval (with request hash and refund amount) the booking becomes 'cancelled'")
        shot(page, "booking-cancelled")

        book(page, "2A")
        item = pending_item(page, "Fare advice")
        item.locator("button", has_text="Approve").click()
        wait_status(page, "confirmed")
        page.select_option("#f-flight", "SA101")
        page.select_option("#f-state", "cancelled")
        page.click("#f-seed")
        page.wait_for_function("document.getElementById('f-msg').innerText.startsWith('Seeded')", timeout=LONG)
        record("Simulated ops-feed event seeded (simulation enabled in this demo run only)")
        item = pending_item(page, "rebooking")
        assert item.locator("button", has_text="Approve").is_enabled()
        wait_status(page, "confirmed")
        record("Poller created a rebooking approval; the booking stays 'confirmed' until a human decides")
        shot(page, "feed-rebooking-awaiting-approval")
        page.click("#f-refresh")
        page.wait_for_function("document.getElementById('f-status').innerText.includes('emitted')", timeout=LONG)
        status_text = page.inner_text("#f-status")
        assert "enabled" in status_text and "quarantined" in status_text, status_text
        record("Ops-feed status shows the poller enabled with cursor and counters (including quarantined)")
        shot(page, "ops-feed-status")
        item.locator("button", has_text="Reject").click()
        page.wait_for_function("document.querySelector('#inbox').innerText.includes('No pending approvals')",
                               timeout=LONG)
        record("Rejecting the rebooking clears it from the inbox; no event approved it")

        page.fill("#s-question", "Can I cancel and get a refund?")
        page.click("#s-build")
        command = page.inner_text("#s-cmd")
        assert command.startswith("curl -s -X POST") and "/support/ask" in command, command
        record("Support box builds a copyable curl command and makes no browser call")
        shot(page, "support-command")
        browser.close()
    assert not dialogs, f"unexpected alerts: {dialogs}"
    record("No error alerts were raised during the whole flow")

    booking_id = re.search(r'"booking_id":"([^"]+)"', command).group(1)
    request = urllib.request.Request(
        SUPPORT + "/support/ask", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"booking_id": booking_id, "question": "Can I cancel and get a refund?"}).encode())
    with urllib.request.urlopen(request, timeout=30) as response:
        answer = json.loads(response.read())
    assert answer["source"] in ("advisor", "fallback") and answer["requires_human_approval"] is True, answer
    assert answer["authority"] == "advisory_only", answer
    record(f"The support command works against support-service (source={answer['source']}, advisory only)")
    (OUT / "assertions.txt").write_text("\n".join(asserted) + "\n", encoding="utf-8")
    print(f"{len(asserted)} states asserted; {count} screenshots in {OUT}")


if __name__ == "__main__":
    main()

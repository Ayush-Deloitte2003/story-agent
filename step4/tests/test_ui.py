import asyncio
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

os.environ.setdefault("AGENT_MODE", "mock")

from app import api

PAGE = Path(api.UI_PAGE)
HTML = PAGE.read_text(encoding="utf-8")
LOGIC = HTML.split("/* LOGIC-START", 1)[1].split("/* LOGIC-END */", 1)[0]
NODE = shutil.which("node")

HARNESS = r"""
const assert = require("assert");
class Store { constructor(){ this.d = {}; } getItem(k){ return k in this.d ? this.d[k] : null; } setItem(k,v){ this.d[k]=v; } }
class Broken { getItem(){ throw new Error("denied"); } setItem(){ throw new Error("denied"); } }
%LOGIC%
// payloads
const cancel = {kind: "cancellation", proposal: {request_hash: "h1", refund_cents: 5000}};
assert.deepStrictEqual(decisionPayload(cancel, "approve"), {decision: "approve", request_hash: "h1", refund_cents: 5000});
assert.deepStrictEqual(decisionPayload(cancel, "reject"), {decision: "reject"});
const rebook = {kind: "rebooking", proposal: {}};
assert.throws(() => decisionPayload(rebook, "approve"));
assert.deepStrictEqual(decisionPayload(rebook, "approve", "SA102"), {decision: "approve", flight_id: "SA102"});
assert.deepStrictEqual(decisionPayload({kind: "booking", proposal: {}}, "approve", "SA999"), {decision: "approve"});
// countdown
const now = Date.parse("2027-01-01T00:00:00Z");
assert.strictEqual(formatCountdown("2027-01-01T00:01:05Z", now), "1m 05s");
assert.strictEqual(formatCountdown("2026-12-31T23:59:59Z", now), "expired");
assert.strictEqual(formatCountdown("garbage", now), "unknown");
// lifecycle
assert.strictEqual(lifecycle({booking: {status: "confirmed"}, workflow: {runtime_status: "COMPLETED"}}).tone, "good");
assert.strictEqual(lifecycle({booking: {status: "expired"}}).tone, "bad");
assert.deepStrictEqual(lifecycle(null), {status: "unknown", runtime: "unknown", tone: "warn"});
// recent bookings: browser-only, bounded, tolerant of bad or unavailable storage
const s = new Store();
rememberBooking(s, "a"); rememberBooking(s, "b"); rememberBooking(s, "a");
assert.deepStrictEqual(loadRecent(s), ["a", "b"]);
assert.deepStrictEqual(forgetBooking(s, "a"), ["b"]);
for (let i = 0; i < 25; i++) rememberBooking(s, "x" + i);
assert.strictEqual(loadRecent(s).length, MAX_RECENT);
s.setItem(RECENT_KEY, "{not json"); assert.deepStrictEqual(loadRecent(s), []);
s.setItem(RECENT_KEY, JSON.stringify([1, "ok", {}])); assert.deepStrictEqual(loadRecent(s), ["ok"]);
assert.deepStrictEqual(loadRecent(new Broken()), []);
rememberBooking(new Broken(), "z");
// stale responses
const latest = makeLatest(); const first = latest.next(); const second = latest.next();
assert.ok(!latest.isCurrent(first) && latest.isCurrent(second));
// support command quoting: a quote in the question cannot break out of the shell string
const cmd = supportCommand("b1", "it's $(rm -rf /)");
assert.ok(cmd.includes("'\\''") && cmd.startsWith("curl -s -X POST http://127.0.0.1:8001/support/ask"));
// keys differ
assert.notStrictEqual(newKey("k", "1"), newKey("k", "2"));
console.log("ok");
"""


class UiPageTests(unittest.TestCase):
    def test_route_serves_the_static_page(self):
        response = api.reviewer_ui()
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.media_type)
        self.assertEqual(response.body.decode(), HTML)
        self.assertIn("/ui", {route.path for route in api.app.routes})

    def test_page_renders_api_text_safely_and_loads_nothing_external(self):
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(forbidden, HTML)
        self.assertEqual(re.findall(r'(?:src|href)="https?://', HTML), [])
        self.assertNotIn("OPENAI", HTML.upper())

    def test_page_only_uses_existing_endpoints_and_states_its_limits(self):
        used = set(re.findall(r'api\("(?:GET|POST)", "(/[a-z\-]+)', HTML))
        self.assertTrue(used <= {"/flights", "/bookings", "/inbox", "/cancellations", "/ops-feed",
                                 "/simulation", "/disruptions"}, used)
        self.assertNotIn('api("POST", "/support', HTML)  # support is a copyable command only
        self.assertNotIn("fetch(\"http", HTML)
        for phrase in ("in this browser only", "no real airline", "no authentication"):
            self.assertIn(phrase, " ".join(HTML.replace("<strong>", "").replace("</strong>", "").split()))

    def test_existing_routes_and_booking_contract_unchanged(self):
        routes = {(m, r.path) for r in api.app.routes for m in getattr(r, "methods", ()) or ()}
        for expected in (("POST", "/bookings"), ("GET", "/bookings/{booking_id}"), ("POST", "/cancellations"),
                         ("GET", "/inbox"), ("POST", "/inbox/{workflow_id}/decision"), ("GET", "/ops-feed/status")):
            self.assertIn(expected, routes)
        booking = next(r for r in api.app.routes if r.path == "/bookings" and "POST" in r.methods)
        self.assertEqual(booking.status_code, 202)


@unittest.skipUnless(NODE or os.getenv("REQUIRE_NODE_TESTS"), "node is not installed; the page logic tests run in CI")
class UiLogicTests(unittest.TestCase):
    def test_page_logic(self):
        self.assertTrue(NODE, "REQUIRE_NODE_TESTS is set but node is missing")
        result = subprocess.run([NODE, "-e", HARNESS.replace("%LOGIC%", LOGIC)], capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()

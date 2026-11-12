"""HTTP client for agent processes running beside a Dapr workflow host."""

import asyncio
import json
import os
import urllib.error
import urllib.request


async def review_with_sidecar(title: str, story: str, request: str) -> dict:
    url = os.getenv("SPECIALIST_AGENT_URL", "http://127.0.0.1:8100/review")
    payload = json.dumps({
        "title": title,
        "story": story,
        "request": request,
    }).encode("utf-8")
    http_request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def send():
        try:
            with urllib.request.urlopen(http_request, timeout=120) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Agent sidecar returned HTTP {exc.code}: {detail[:500]}"
            ) from exc
        result = json.loads(body)
        if not isinstance(result, dict):
            raise RuntimeError("Agent sidecar returned a non-object response.")
        return result

    return await asyncio.to_thread(send)

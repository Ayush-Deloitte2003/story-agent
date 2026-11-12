"""HTTP client for writer and quality-review agent sidecars."""

import asyncio
import json
import os
import urllib.error
import urllib.request

from app.quality_review import parse_quality_review_response


async def _post_json(url: str, payload: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def send():
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
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


async def draft_with_sidecar(request: str, revisions: list[dict]) -> dict:
    url = os.getenv("WRITER_AGENT_URL", "http://127.0.0.1:8100/draft")
    result = await _post_json(
        url,
        {"request": request, "revisions": revisions},
        timeout=300,
    )
    if not isinstance(result.get("title"), str) or not isinstance(result.get("story"), str):
        raise RuntimeError("Writer sidecar returned an invalid draft.")
    if isinstance(result.get("llm_calls"), bool) or not isinstance(result.get("llm_calls"), int):
        raise RuntimeError("Writer sidecar returned an invalid LLM call count.")
    return result


async def review_quality_with_sidecar(title: str, story: str, request: str) -> dict:
    url = os.getenv("QUALITY_REVIEW_AGENT_URL", "http://127.0.0.1:8101/review")
    result = await _post_json(
        url, {"title": title, "story": story, "request": request}, timeout=120
    )
    return parse_quality_review_response(json.dumps(result))

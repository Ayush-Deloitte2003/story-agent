"""Single-request OpenAI JSON client for the bounded Step 4 advisor processes."""

import json
import os
import threading
import urllib.error
import urllib.request

MAX_PROVIDER_REQUESTS = 5
MAX_RESPONSE_BYTES = 16_384
_provider_requests = 0
_request_lock = threading.Lock()


class LLMRequestError(RuntimeError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def agent_mode() -> str:
    mode = os.getenv("AGENT_MODE", "mock")
    if mode not in ("mock", "llm"):
        raise ValueError("AGENT_MODE must be 'mock' or 'llm'")
    return mode


def provider_request_count() -> int:
    with _request_lock:
        return _provider_requests


def complete_json(system_prompt: str, user_payload: dict) -> dict:
    """Make one non-retrying provider request and return a JSON object."""
    global _provider_requests
    if agent_mode() != "llm":
        raise LLMRequestError("LLM calls are disabled outside AGENT_MODE=llm")
    limit = min(MAX_PROVIDER_REQUESTS, int(os.getenv("LLM_REQUEST_LIMIT", str(MAX_PROVIDER_REQUESTS))))
    if limit < 1:
        raise ValueError("LLM_REQUEST_LIMIT must be positive")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise LLMRequestError("OPENAI_API_KEY is required when AGENT_MODE=llm")

    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "temperature": 0,
        "max_tokens": 300,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, separators=(",", ":"))},
        ],
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with _request_lock:
        if _provider_requests >= limit:
            raise LLMRequestError("LLM request cap reached")
        _provider_requests += 1
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(request, timeout=20) as response:
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise LLMRequestError(f"LLM provider returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LLMRequestError("LLM provider request failed") from None
    if len(response_body) > MAX_RESPONSE_BYTES:
        raise LLMRequestError("LLM provider response exceeded the size limit")
    try:
        envelope = json.loads(response_body)
        content = envelope["choices"][0]["message"]["content"]
        result = json.loads(content)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise LLMRequestError("LLM provider returned an invalid JSON response") from None
    if not isinstance(result, dict):
        raise LLMRequestError("LLM response must be a JSON object")
    return result

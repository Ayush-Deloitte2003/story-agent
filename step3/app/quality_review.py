"""Validation for advisory story-quality reviewer output."""

import json


def parse_quality_review_response(content: str) -> dict:
    try:
        result = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise ValueError("Quality reviewer returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise ValueError("Quality reviewer response must be a JSON object.")

    score = result.get("score")
    if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
        raise ValueError("Quality reviewer score must be an integer from 1 to 5.")
    if result.get("recommendation") not in ("ready", "revise"):
        raise ValueError("Quality reviewer recommendation must be ready or revise.")
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        raise ValueError("Quality reviewer summary must be a non-empty string.")
    for field in ("strengths", "issues", "suggestions"):
        values = result.get(field)
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ValueError(f"Quality reviewer {field} must be an array of strings.")
    return result

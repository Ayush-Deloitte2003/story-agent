"""Strict response validation for independent story specialist agents."""

import json


def parse_continuity_check_response(content: str) -> dict:
    result = _parse_object(content, "Continuity checker")
    if not isinstance(result.get("consistent"), bool):
        raise ValueError("Continuity checker consistent must be a boolean.")
    _require_summary(result, "Continuity checker")
    findings = result.get("findings")
    if not isinstance(findings, list):
        raise ValueError("Continuity checker findings must be an array.")
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("Continuity checker findings must contain objects.")
        if finding.get("category") not in ("characters", "setting", "timeline", "plot", "other"):
            raise ValueError("Continuity finding category is invalid.")
        for field in ("evidence", "issue", "suggestion"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                raise ValueError(f"Continuity finding {field} must be a non-empty string.")
    return result


def parse_brief_compliance_response(content: str) -> dict:
    result = _parse_object(content, "Brief-compliance reviewer")
    if not isinstance(result.get("compliant"), bool):
        raise ValueError("Brief-compliance reviewer compliant must be a boolean.")
    _require_summary(result, "Brief-compliance reviewer")
    requirements = result.get("requirements")
    if not isinstance(requirements, list):
        raise ValueError("Brief-compliance requirements must be an array.")
    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise ValueError("Brief-compliance requirements must contain objects.")
        if not isinstance(requirement.get("requirement"), str) or not requirement["requirement"].strip():
            raise ValueError("Each checked requirement must be a non-empty string.")
        if requirement.get("status") not in ("met", "partially_met", "not_met"):
            raise ValueError("Brief-compliance requirement status is invalid.")
        if not isinstance(requirement.get("evidence"), str):
            raise ValueError("Brief-compliance requirement evidence must be a string.")
    return result


def _parse_object(content: str, reviewer: str) -> dict:
    try:
        result = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{reviewer} returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{reviewer} response must be a JSON object.")
    return result


def _require_summary(result: dict, reviewer: str) -> None:
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        raise ValueError(f"{reviewer} summary must be a non-empty string.")

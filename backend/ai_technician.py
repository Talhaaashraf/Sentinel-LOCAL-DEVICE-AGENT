"""One-shot AI technician (explain a snapshot, explain logs, chat) on the configured LLM provider."""

import json
import logging

from .llm import LLMModelError, LLMUnavailableError, get_provider

DIAGNOSIS_SYSTEM_PROMPT = (
    "You are an experienced IT help desk technician analyzing a device's health data. "
    "Explain issues in plain, non-technical language first, then give a technical root-cause "
    "explanation, then concrete step-by-step recommendations a normal user can follow. "
    "Return valid JSON only."
)
LOG_SYSTEM_PROMPT = (
    "You are an experienced IT support engineer analyzing recent operating system event log "
    "entries (Windows Event Viewer, journald, or the macOS unified log). Identify the most "
    "likely root cause behind any critical or error patterns, explain it in plain language "
    "first, then give the technical detail, then concrete remediation steps. If the logs show "
    "no meaningful problem, say so plainly. Return valid JSON only."
)
CHAT_SYSTEM_PROMPT = DIAGNOSIS_SYSTEM_PROMPT + " Return valid JSON with plain_explanation, technical_root_cause, and recommendations fields."

logger = logging.getLogger(__name__)

# Kept under their historical names so callers did not have to change.
AIUnavailableError = LLMUnavailableError
AIModelUnavailableError = LLMModelError


def ai_status():
    try:
        return get_provider().status()
    except LLMUnavailableError as error:
        return {"available": False, "provider": "unknown", "model": None, "message": str(error)}


def is_ai_available():
    return bool(ai_status().get("available"))


def _completion(messages):
    return get_provider().chat(messages, json_mode=True)["content"]


def _parse_diagnosis(raw):
    try:
        result = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {
            "summary": raw,
            "root_cause": "The technician returned an unstructured response.",
            "severity": "medium",
            "steps": [
                "Review the current alerts and metrics above.",
                "Ask the technician a follow-up question for a more focused explanation.",
            ],
            "suggested_actions": [],
        }
    return {
        "summary": str(result.get("summary", "No summary returned.")),
        "root_cause": str(result.get("root_cause", "Unknown")),
        "severity": str(result.get("severity", "medium")),
        "steps": [str(step) for step in result.get("steps", [])],
        "suggested_actions": list(result.get("suggested_actions", [])),
    }


def diagnose(report, alert_history):
    schema_hint = " Use this schema: {summary, root_cause, severity, steps, suggested_actions}."
    raw = _completion([
        {"role": "system", "content": DIAGNOSIS_SYSTEM_PROMPT + schema_hint},
        {"role": "user", "content": json.dumps({"diagnostics": report, "recent_alerts": alert_history}, default=str)},
    ])
    return _parse_diagnosis(raw)


def diagnose_logs(event_log_report):
    schema_hint = " Use this schema: {summary, root_cause, severity, steps, suggested_actions}."
    raw = _completion([
        {"role": "system", "content": LOG_SYSTEM_PROMPT + schema_hint},
        {"role": "user", "content": json.dumps({"event_logs": event_log_report}, default=str)},
    ])
    return _parse_diagnosis(raw)


def _parse_chat_reply(raw):
    text = raw.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        result = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {
            "plain_explanation": raw,
            "technical_root_cause": "The technician returned an unstructured response.",
            "recommendations": [],
        }
    return {
        "plain_explanation": str(result.get("plain_explanation", result.get("summary", "No plain-language explanation returned."))),
        "technical_root_cause": str(result.get("technical_root_cause", result.get("root_cause", "Unknown"))),
        "recommendations": [str(item) for item in result.get("recommendations", result.get("steps", []))],
    }


def chat(message, report, conversation_history):
    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    messages.extend({"role": item["role"], "content": str(item.get("content", ""))} for item in conversation_history[-6:])
    messages.append({"role": "user", "content": json.dumps({"question": message, "current_diagnostics": report}, default=str)})
    return _parse_chat_reply(_completion(messages))

"""Groq-backed technician with a clean failure boundary for local-only mode."""

import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
SYSTEM_PROMPT = "You are an experienced IT help desk technician analyzing a Windows device's health data. Explain issues in plain, non-technical language first, then give a technical root-cause explanation, then concrete step-by-step recommendations a normal user can follow. Return valid JSON only."
logger = logging.getLogger(__name__)


class AIUnavailableError(RuntimeError):
    pass


class AIModelUnavailableError(AIUnavailableError):
    """Raised when Groq rejects the configured model or request."""

    public_error = "AI model unavailable, please check GROQ_MODEL in .env"


def _client():
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        raise AIUnavailableError("AI technician unavailable: GROQ_API_KEY is not configured")
    try:
        from groq import Groq
        return Groq(api_key=key)
    except Exception as error:
        raise AIUnavailableError(f"AI technician unavailable: {error}") from error


def is_ai_available():
    return bool(os.getenv("GROQ_API_KEY", "").strip())


def _completion(messages, structured=False):
    kwargs = {"model": GROQ_MODEL, "messages": messages, "temperature": 0.2}
    if structured:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        response = _client().chat.completions.create(**kwargs)
        return response.choices[0].message.content
    except Exception as error:
        status_code = getattr(error, "status_code", None)
        error_name = type(error).__name__
        if error_name == "NotFoundError" or status_code is not None and 400 <= status_code < 500:
            logger.exception("Groq rejected model %s with a client error", GROQ_MODEL)
            raise AIModelUnavailableError(AIModelUnavailableError.public_error) from error
        logger.exception("Groq request failed for model %s", GROQ_MODEL)
        raise AIUnavailableError("AI technician is temporarily unavailable") from error


def diagnose(report, alert_history):
    raw = _completion([{"role": "system", "content": SYSTEM_PROMPT + " Use this schema: {summary, root_cause, severity, steps, suggested_actions}."}, {"role": "user", "content": json.dumps({"diagnostics": report, "recent_alerts": alert_history})}], structured=True)
    try:
        result = json.loads(raw)
        return {"summary": str(result.get("summary", "No summary returned.")), "root_cause": str(result.get("root_cause", "Unknown")), "severity": str(result.get("severity", "medium")), "steps": list(result.get("steps", [])), "suggested_actions": list(result.get("suggested_actions", []))}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"summary": raw, "root_cause": "The technician returned an unstructured response.", "severity": "medium", "steps": ["Review the current alerts and metrics above.", "Ask the technician a follow-up question for a more focused explanation."], "suggested_actions": []}


def _structured_chat(raw):
    text = raw.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        result = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"plain_explanation": raw, "technical_root_cause": "The technician returned an unstructured response.", "recommendations": []}
    return {
        "plain_explanation": str(result.get("plain_explanation", result.get("summary", "No plain-language explanation returned."))),
        "technical_root_cause": str(result.get("technical_root_cause", result.get("root_cause", "Unknown"))),
        "recommendations": [str(item) for item in result.get("recommendations", result.get("steps", []))],
    }


def chat(message, report, conversation_history):
    messages = [{"role": "system", "content": SYSTEM_PROMPT + " Return valid JSON with plain_explanation, technical_root_cause, and recommendations fields."}]
    messages.extend(conversation_history[-6:])
    messages.append({"role": "user", "content": json.dumps({"question": message, "current_diagnostics": report})})
    return _structured_chat(_completion(messages, structured=True))

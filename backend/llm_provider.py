"""LLM provider abstraction: local Ollama (default, free) or Groq (cloud, optional).

Select with LLM_PROVIDER=ollama|groq in .env. Both expose the same chat() call:
messages in OpenAI format (tool arguments as JSON strings, tool results with
tool_call_id), returning {"content": str, "tool_calls": [{"id", "name", "arguments": dict}]}.

Ollama defaults: OLLAMA_URL=http://127.0.0.1:11434, OLLAMA_MODEL=qwen2.5:3b-instruct
(runs on a CPU-only PC with 16 GB RAM and supports native tool calling).
"""

import json
import logging
import os
import time
import uuid

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_MODEL = "qwen2.5:3b-instruct"
_status_cache = {"checked_at": 0.0, "value": None}


class LLMUnavailableError(RuntimeError):
    pass


def provider_name():
    configured = os.getenv("LLM_PROVIDER", "").strip().lower()
    if configured in ("ollama", "groq"):
        return configured
    return "groq" if os.getenv("GROQ_API_KEY", "").strip() and not os.getenv("OLLAMA_MODEL") else "ollama"


def ollama_url():
    return os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")


def ollama_model():
    return os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL).strip() or DEFAULT_OLLAMA_MODEL


def groq_model():
    return os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")


def status(force=False):
    """{"provider", "model", "available", "detail"} — cached for 20 seconds."""
    now = time.time()
    if not force and _status_cache["value"] and now - _status_cache["checked_at"] < 20:
        return _status_cache["value"]
    name = provider_name()
    if name == "groq":
        available = bool(os.getenv("GROQ_API_KEY", "").strip())
        value = {"provider": "groq", "model": groq_model(), "available": available,
                 "detail": "Groq API key configured" if available else "Set GROQ_API_KEY in .env"}
    else:
        model = ollama_model()
        try:
            response = httpx.get(f"{ollama_url()}/api/tags", timeout=2.5)
            names = [item.get("name", "") for item in response.json().get("models", [])]
            pulled = model in names or f"{model}:latest" in names
            available = pulled
            detail = "Ollama ready" if pulled else f"Ollama is running but model {model} is not pulled. Run: ollama pull {model}"
        except (httpx.HTTPError, ValueError):
            available = False
            detail = f"Ollama not reachable at {ollama_url()}. Install from https://ollama.com and run: ollama pull {model}"
        value = {"provider": "ollama", "model": model, "available": available, "detail": detail}
    _status_cache.update(checked_at=now, value=value)
    return value


def is_available():
    return status()["available"]


# ============================================================================
# Chat
# ============================================================================

def chat(messages, tools=None, json_mode=False, temperature=0.2, timeout=240):
    if provider_name() == "groq":
        return _chat_groq(messages, tools, json_mode, temperature)
    return _chat_ollama(messages, tools, json_mode, temperature, timeout)


def _to_ollama_messages(messages):
    converted = []
    for message in messages:
        item = {"role": message["role"], "content": message.get("content") or ""}
        if message.get("tool_calls"):
            item["tool_calls"] = [
                {"function": {"name": call["function"]["name"], "arguments": _as_dict(call["function"].get("arguments"))}}
                for call in message["tool_calls"]
            ]
        if message["role"] == "tool" and message.get("name"):
            item["tool_name"] = message["name"]
        converted.append(item)
    return converted


def _as_dict(arguments):
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _chat_ollama(messages, tools, json_mode, temperature, timeout):
    body = {
        "model": ollama_model(),
        "messages": _to_ollama_messages(messages),
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192"))},
    }
    if tools:
        body["tools"] = tools
    if json_mode and not tools:
        body["format"] = "json"
    try:
        response = httpx.post(f"{ollama_url()}/api/chat", json=body, timeout=timeout)
        response.raise_for_status()
        message = response.json().get("message", {})
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("Ollama chat failed: %s", error)
        raise LLMUnavailableError(f"Ollama request failed: {error}") from error
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        calls.append({"id": f"call_{uuid.uuid4().hex[:8]}", "name": function.get("name"), "arguments": _as_dict(function.get("arguments"))})
    return {"content": message.get("content") or "", "tool_calls": calls}


def _chat_groq(messages, tools, json_mode, temperature):
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        raise LLMUnavailableError("GROQ_API_KEY is not configured")
    try:
        from groq import Groq
        kwargs = {"model": groq_model(), "messages": messages, "temperature": temperature}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        elif json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = Groq(api_key=key).chat.completions.create(**kwargs)
    except Exception as error:
        logger.warning("Groq chat failed: %s", error)
        raise LLMUnavailableError(f"Groq request failed: {error}") from error
    message = response.choices[0].message
    calls = []
    for call in getattr(message, "tool_calls", None) or []:
        calls.append({"id": call.id, "name": call.function.name, "arguments": _as_dict(call.function.arguments)})
    return {"content": message.content or "", "tool_calls": calls}


def complete_json(system_prompt, user_content, temperature=0.2):
    """Single-shot JSON answer (used by the existing diagnose/chat endpoints)."""
    result = chat(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}],
        json_mode=True, temperature=temperature,
    )
    return result["content"]

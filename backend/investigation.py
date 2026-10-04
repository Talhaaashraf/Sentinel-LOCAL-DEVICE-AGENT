"""Agentic investigation loop: problem -> evidence -> diagnosis -> proposed fixes.

Flow for one investigation:
  1. Correct spelling and classify the problem into playbook categories.
  2. Run the playbook's read tools on the agent (deterministic evidence, works
     even when the LLM is small or offline).
  3. If an LLM is available: give it the evidence, device context and similar
     past cases; let it call more read tools (auto-run) or propose change/stress
     tools (queued for technician approval, never run automatically); stop at
     MAX_STEPS and ask for a final JSON diagnosis.
  4. Merge rule-based findings, create approval requests for proposed fixes, and
     wait for the technician to record the outcome (which feeds decision memory).
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from . import command_store, decision_memory, health_score, llm_provider, playbooks, tool_policy
from .device_store import _connect as _device_connect

logger = logging.getLogger(__name__)

MAX_STEPS = 8
MAX_TOOL_CALLS_PER_STEP = 3
RESULT_CHARS_FOR_LLM = 3500

SYSTEM_PROMPT = """You are Sentinel, a senior IT support engineer diagnosing a laptop remotely for a technician.
You can call diagnostic tools that run on the laptop. Read-only tools run immediately.
Tools marked NEEDS TECHNICIAN APPROVAL are only proposed: calling one puts it in the technician's approval queue; it does not run now.
Rules:
- Base every conclusion on tool evidence. Do not invent numbers.
- Call more read-only tools only if the evidence so far is not enough (at most a few calls).
- Propose a fix tool only when the evidence supports it, and explain why in one line.
- Prefer the least risky fix first (e.g. clean cache before uninstall, DISM before reinstall).
- The technician may type with spelling mistakes; use the corrected text.
When you are done, reply with ONLY a JSON object:
{"summary": "...", "root_cause": "...", "severity": "low|medium|high|critical",
 "evidence": ["short fact from a tool", "..."], "steps": ["what the technician should do next", "..."]}"""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _connect():
    connection = _device_connect()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS investigations (
            id TEXT PRI
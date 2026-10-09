"""The agentic troubleshooting loop.

1. Baseline: run system_overview plus the matched playbook's tools (deterministic).
2. Investigate: the LLM calls further read-only tools until it has enough evidence.
3. Report: a JSON-mode call turns the evidence into root cause + proposed fixes,
   where fixes may only reference allow-listed remediation tools.
4. Fix (on operator click, or automatically up to AUTO_FIX_MAX_RISK) and
   re-verify with fresh evidence.

The LLM can never execute a fix itself: during investigation it only sees
read-risk tool schemas, and every proposed fix is validated against the registry.
"""

import asyncio
import json
import logging
import os
import re

from agent.toolkit import get_tool, list_tools, risk_rank, tool_schemas
from agent.toolkit.registry import validate_args

from ..alert_store import get_alerts
from ..learning import model_builder
from ..learning import store as learning_store
from ..llm import LLMUnavailableError, get_provider
from . import events, session_store
from .executor import execute, resolve_target
from .playbooks import baseline_tools, get_playbook, match_playbook

logger = logging.getLogger(__name__)

# Kept small on purpose: on a laptop CPU, prompt length dominates how long each LLM call takes.
TOOL_RESULT_CHARS = 1200
REPORT_EVIDENCE_CHARS = 700
MAX_CALLS_PER_TURN = 3
SEVERITIES = ("low", "medium", "high", "critical")
CONFIDENCES = ("low", "medium", "high")

_session_locks = {}

INVESTIGATE_PROMPT = """You are Sentinel, an expert IT support technician remotely diagnosing a {os_type} computer.
Your job: find the ROOT CAUSE of the user's problem using the diagnostic tools provided.
Rules:
- Base every conclusion on tool output. Never invent numbers or facts.
- Baseline evidence is already collected below; do not re-run those exact checks.
- Call at most 2 tools at a time. Only read-only diagnostics are available now; fixes come later.
- Stop as soon as the evidence clearly explains the problem (usually 1-4 extra tool calls).
- When done, reply in plain text with a short summary of your findings and NO tool call."""

REPORT_PROMPT = """You are Sentinel, a senior IT support technician. Write the final diagnosis for a {os_type} computer as JSON.
Use ONLY the evidence provided. Explain in simple language a non-technical user understands.
JSON schema:
{{
  "issue_summary": "one sentence restating the user's problem",
  "root_cause": "the most likely root cause, citing the evidence",
  "confidence": "low|medium|high",
  "severity": "low|medium|high|critical",
  "evidence": ["short fact from a tool result", "..."],
  "proposed_fixes": [{{"tool_id": "<fix tool id>", "args": {{}}, "why": "why this fix addresses the root cause"}}],
  "manual_steps": ["step the user must do by hand, if any"]
}}
proposed_fixes may ONLY use these fix tools (id, risk, description, params). Use [] if none applies; never suggest kill_process for system processes.
If one of these tools can do a step for the user (e.g. flushing DNS), put it in proposed_fixes, NOT in manual_steps:
{fix_catalog}"""

# Small models often write a fix as a manual instruction ("run ipconfig /flushdns").
# These patterns turn such steps into one-click fixes when a matching tool exists.
MANUAL_STEP_FIXES = (
    (re.compile(r"flushdns|flush\w*\s+(the\s+)?dns|dns\s+cache", re.I), "flush_dns"),
    (re.compile(r"ipconfig\s*/renew|renew\w*\s+(the\s+|your\s+)?ip", re.I), "renew_ip"),
    (re.compile(r"winsock|netsh\s+int\w*\s+ip\s+reset", re.I), "reset_network_stack"),
    (re.compile(r"sfc\s*/scannow|system file checker", re.I), "repair_system_files"),
    (re.compile(r"dism\b", re.I), "repair_windows_image"),
    (re.compile(r"(delete|clear|clean)\w*\s+(the\s+|all\s+)?temp", re.I), "clear_temp_files"),
    (re.compile(r"(empty|clear)\w*\s+(the\s+)?(recycle\s*bin|trash)", re.I), "empty_recycle_bin"),
    (re.compile(r"print\s+queue|stuck\s+print|spooler", re.I), "clear_print_queue"),
    (re.compile(r"w32tm|sync\w*\s+(the\s+)?(system\s+)?(time|clock)", re.I), "sync_time"),
    (re.compile(r"softwaredistribution|update\s+cache", re.I), "clear_windows_update_cache"),
)

VERIFY_PROMPT = """You are Sentinel, an IT technician verifying whether a fix worked on a {os_type} computer.
Compare the original diagnosis with the fresh evidence collected after the fix. Reply as JSON:
{{"resolved": true|false, "explanation": "plain-language verdict citing the fresh evidence", "next_steps": ["..."]}}"""


def auto_fix_max_risk():
    value = os.getenv("AUTO_FIX_MAX_RISK", "none").strip().lower()
    return value if value in ("low", "medium", "high") else None


def max_investigation_steps():
    try:
        return max(0, min(12, int(os.getenv("AGENT_MAX_STEPS", "3"))))
    except ValueError:
        return 3


def _compact(result, limit=TOOL_RESULT_CHARS):
    payload = json.dumps({"ok": result.get("ok"), "summary": result.get("summary"), "data": result.get("data")}, default=str)
    return payload if len(payload) <= limit else payload[:limit] + "...(truncated)"


def _lock(session_id):
    return _session_locks.setdefault(session_id, asyncio.Lock())


def _publish_status(session_id, status, message=""):
    session_store.update_session(session_id, status=status)
    events.publish(session_id, {"type": "status", "status": status, "message": message})


async def _run_step(session_id, target, tool_id, args, max_risk="read", kind="tool"):
    events.publish(session_id, {"type": "step_started", "kind": kind, "tool_id": tool_id, "args": args})
    result = await execute(target, tool_id, args, max_risk, session_id)
    step = session_store.add_step(session_id, kind, tool_id, args, result)
    events.publish(session_id, {"type": "step", "step": step})
    return result


def _think(session_id, text):
    if text:
        step = session_store.add_step(session_id, "thought", text=text[:2000])
        events.publish(session_id, {"type": "step", "step": step})


def parse_inline_tool_calls(content, allowed):
    """Small local models sometimes write the tool call as JSON text instead of a real call."""
    calls, decoder, index = [], json.JSONDecoder(), 0
    text = content or ""
    while (index := text.find("{", index)) != -1:
        try:
            obj, end = decoder.raw_decode(text, index)
        except ValueError:
            index += 1
            continue
        index = end
        if isinstance(obj, dict) and obj.get("name") in allowed:
            arguments = obj.get("arguments", obj.get("parameters", {}))
            calls.append({"id": f"inline-{len(calls)}", "name": obj["name"], "arguments": arguments if isinstance(arguments, dict) else {}})
    return calls


def _parse_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                pass
    return {}


def fix_catalog(os_type):
    """One compact line per fix tool: `- id (risk) args {...}: first sentence of the description`."""
    lines = []
    for item in list_tools(os_type, max_risk="high", min_risk="low"):
        params = ", ".join(f"{name}: {spec.get('type')}" for name, spec in item.params.items())
        summary = item.description.split(". ")[0].rstrip(".")
        lines.append(f"- {item.id} ({item.risk})" + (f" args {{{params}}}" if params else "") + f": {summary}")
    return "\n".join(lines)


def validate_fixes(raw_fixes, os_type):
    """Keep only fixes that name a real remediation tool for this OS with valid arguments."""
    fixes, seen = [], set()
    for raw in raw_fixes if isinstance(raw_fixes, list) else []:
        if not isinstance(raw, dict):
            continue
        item = get_tool(str(raw.get("tool_id", "")).strip())
        if item is None or item.risk == "read" or os_type not in item.os:
            continue
        try:
            args = validate_args(item, raw.get("args") or {})
        except ValueError:
            continue
        key = (item.id, json.dumps(args, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        fixes.append({"tool_id": item.id, "args": args, "risk": item.risk, "description": item.description, "why": str(raw.get("why", ""))[:500], "status": "proposed", "result": None})
    return fixes


def promote_manual_steps(manual_steps, fixes, os_type):
    """Turn manual instructions that a fix tool can perform into proposed fixes."""
    remaining, present = [], {fix["tool_id"] for fix in fixes}
    for step in manual_steps:
        tool_id = next((tool_id for pattern, tool_id in MANUAL_STEP_FIXES if pattern.search(step)), None)
        item = get_tool(tool_id) if tool_id else None
        if item is None or os_type not in item.os or item.required:
            remaining.append(step)
            continue
        if tool_id not in present:
            present.add(tool_id)
            fixes.append({"tool_id": tool_id, "args": {}, "risk": item.risk, "description": item.description, "why": f"Sentinel can do this step for you: {step[:300]}", "status": "proposed", "result": None})
    return remaining, fixes


def normalize_report(raw, issue, os_type, fallback_note=None):
    severity = str(raw.get("severity", "medium")).lower()
    confidence = str(raw.get("confidence", "medium")).lower()
    manual_steps, fixes = promote_manual_steps(
        [str(item)[:400] for item in (raw.get("manual_steps") or [])][:10],
        validate_fixes(raw.get("proposed_fixes"), os_type),
        os_type,
    )
    return {
        "issue_summary": str(raw.get("issue_summary") or issue)[:500],
        "root_cause": str(raw.get("root_cause") or fallback_note or "The agent could not determine a root cause from the evidence.")[:1500],
        "confidence": confidence if confidence in CONFIDENCES else "medium",
        "severity": severity if severity in SEVERITIES else "medium",
        "evidence": [str(item)[:300] for item in (raw.get("evidence") or [])][:10],
        "proposed_fixes": fixes,
        "manual_steps": manual_steps,
    }


def _past_cases_block(similar):
    if not similar:
        return ""
    lines = []
    for case in similar:
        fixes = [fix["tool_id"] for fix in case.get("fixes", []) if fix.get("status") == "applied"]
        lines.append(f"- \"{case['issue'][:120]}\" ({case.get('os_type')}): cause was {case.get('effective_root_cause', '')[:200]}" + (f"; fixed by {', '.join(fixes)}" if fixes else ""))
    return "\n\nSimilar tickets resolved before (use as hints, but trust today's evidence first):\n" + "\n".join(lines)


def add_learned_fixes(report, learned, os_type):
    """Propose fixes that resolved most similar past tickets if the model did not."""
    present = {fix["tool_id"] for fix in report["proposed_fixes"]}
    for item in learned:
        tool = get_tool(item["tool_id"])
        if tool is None or item["tool_id"] in present or os_type not in tool.os or tool.required:
            continue
        report["proposed_fixes"].append({
            "tool_id": tool.id, "args": {}, "risk": tool.risk, "description": tool.description,
            "why": f"Learned: this fix resolved {item['resolved']} of {item['attempts']} similar past tickets.",
            "status": "proposed", "result": None, "learned": True,
        })
    return report


async def learn_from(session_id):
    """Record the session as a learned case and rebuild the sentinel-tech model when due."""
    try:
        session = session_store.get_session(session_id, include_steps=False)
        if session and session.get("report"):
            await asyncio.to_thread(learning_store.record_session, session)
            info = await asyncio.to_thread(model_builder.maybe_rebuild)
            if info:
                logger.info("Rebuilt %s v%s from %s trusted cases", info["model"], info["version"], info["trusted_cases"])
                refresh = getattr(get_provider(), "refresh", None)
                if refresh:
                    refresh()
    except Exception:
        logger.exception("Learning from session %s failed", session_id)


def _evidence_block(evidence, limit):
    return "\n".join(f"- {tool_id} {json.dumps(args) if args else ''}: {_compact(result, limit)}" for tool_id, args, result in evidence)


async def _chat(messages, **kwargs):
    return await asyncio.to_thread(get_provider().chat, messages, **kwargs)


async def run_diagnosis(session_id):
    session = session_store.get_session(session_id, include_steps=False)
    target = resolve_target(session["device_id"])
    if target is None:
        session_store.update_session(session_id, status="failed", error="Device no longer exists")
        events.publish(session_id, {"type": "error", "message": "Device no longer exists"})
        return
    async with _lock(session_id):
        try:
            await _diagnose(session, target)
        except LLMUnavailableError as error:
            logger.warning("Troubleshoot %s: LLM unavailable: %s", session_id, error)
            session_store.update_session(session_id, status="failed", error=str(error))
            events.publish(session_id, {"type": "error", "message": str(error)})
        except Exception as error:
            logger.exception("Troubleshoot session %s failed", session_id)
            session_store.update_session(session_id, status="failed", error=f"{type(error).__name__}: {error}")
            events.publish(session_id, {"type": "error", "message": "The troubleshooting agent hit an internal error. See server logs."})
    await _auto_fix(session_id)
    await learn_from(session_id)


async def _diagnose(session, target):
    session_id, issue = session["session_id"], session["issue"]
    _publish_status(session_id, "running", f"Collecting baseline evidence from {target.label}")
    status = await asyncio.to_thread(get_provider().status)
    if not status.get("available"):
        raise LLMUnavailableError(status.get("message", "AI provider unavailable"))

    playbook = get_playbook(session.get("playbook_id")) or match_playbook(issue)
    if playbook:
        _think(session_id, f"Using the '{playbook['title']}' playbook for the baseline checks.")
    evidence = []
    for tool_id, args in baseline_tools(playbook, target.os_type):
        evidence.append((tool_id, args, await _run_step(session_id, target, tool_id, args)))

    alerts = get_alerts(10, "local-server" if target.is_local else target.device_id)
    alert_lines = "\n".join(f"- [{alert['severity']}] {alert['category']}: {alert['description']}" for alert in alerts) or "- none"
    similar = await asyncio.to_thread(learning_store.find_similar, issue, target.os_type, 3)
    learned_fixes = await asyncio.to_thread(learning_store.learned_fixes_for, issue, target.os_type)
    if similar:
        _think(session_id, f"Memory: found {len(similar)} similar ticket(s) solved before; using them as hints.")
    past_cases = _past_cases_block(similar)
    messages = [
        {"role": "system", "content": INVESTIGATE_PROMPT.format(os_type=target.os_type)},
        {"role": "user", "content": f"User's problem: {issue}\n\nBaseline evidence:\n{_evidence_block(evidence, TOOL_RESULT_CHARS)}\n\nRecent monitoring alerts:\n{alert_lines}{past_cases}"},
    ]
    schemas = tool_schemas(target.os_type, max_risk="read")
    allowed = {schema["function"]["name"] for schema in schemas}
    executed = {(tool_id, json.dumps(args, sort_keys=True)) for tool_id, args, _ in evidence}
    notes = ""

    _publish_status(session_id, "running", "AI is investigating")
    for _ in range(max_investigation_steps()):
        reply = await _chat(messages, tools=schemas)
        calls = reply["tool_calls"] or parse_inline_tool_calls(reply["content"], allowed)
        if not calls:
            notes = reply["content"]
            _think(session_id, notes)
            break
        if reply["content"]:
            _think(session_id, reply["content"])
        calls = calls[:MAX_CALLS_PER_TURN]
        messages.append({"role": "assistant", "content": reply["content"], "tool_calls": calls})
        for call in calls:
            key = (call["name"], json.dumps(call.get("arguments") or {}, sort_keys=True))
            if call["name"] not in allowed:
                content = json.dumps({"ok": False, "summary": f"{call['name']} is not an available diagnostic tool"})
            elif key in executed:
                content = json.dumps({"ok": True, "summary": "Already collected above; use that evidence."})
            else:
                executed.add(key)
                result = await _run_step(session_id, target, call["name"], call.get("arguments") or {})
                evidence.append((call["name"], call.get("arguments") or {}, result))
                content = _compact(result)
            messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": content})

    _publish_status(session_id, "running", "Writing the diagnosis")
    report_messages = [
        {"role": "system", "content": REPORT_PROMPT.format(os_type=target.os_type, fix_catalog=fix_catalog(target.os_type))},
        {"role": "user", "content": f"User's problem: {issue}\n\nEvidence:\n{_evidence_block(evidence, REPORT_EVIDENCE_CHARS)}\n\nInvestigator notes: {notes or 'none'}{past_cases}"},
    ]
    reply = await _chat(report_messages, json_mode=True)
    report = normalize_report(_parse_json(reply["content"]), issue, target.os_type, fallback_note=notes)
    report = add_learned_fixes(report, learned_fixes, target.os_type)
    report["similar_cases"] = [{"session_id": case["session_id"], "issue": case["issue"][:160], "root_cause": (case.get("effective_root_cause") or "")[:240], "outcome": case["outcome"]} for case in similar]
    session_store.update_session(session_id, status="diagnosed", report=report)
    events.publish(session_id, {"type": "report", "report": report})
    _publish_status(session_id, "diagnosed", "Diagnosis ready")


async def _auto_fix(session_id):
    limit = auto_fix_max_risk()
    session = session_store.get_session(session_id, include_steps=False)
    if not limit or not session or session["status"] != "diagnosed":
        return
    for index, fix in enumerate((session["report"] or {}).get("proposed_fixes", [])):
        if risk_rank(fix["risk"]) <= risk_rank(limit):
            await apply_fix(session_id, index, approved_by="auto")


async def apply_fix(session_id, index, approved_by="operator"):
    async with _lock(session_id):
        session = session_store.get_session(session_id, include_steps=False)
        report = (session or {}).get("report") or {}
        fixes = report.get("proposed_fixes", [])
        if not 0 <= index < len(fixes):
            raise ValueError("Unknown fix")
        fix = fixes[index]
        if fix["status"] in ("running", "applied"):
            return fix
        target = resolve_target(session["device_id"])
        if target is None:
            raise ValueError("Device no longer exists")

        fix["status"] = "running"
        session_store.update_session(session_id, status="fixing", report=report)
        events.publish(session_id, {"type": "fix", "index": index, "fix": fix})
        result = await _run_step(session_id, target, fix["tool_id"], fix["args"], max_risk="high", kind="fix")
        fix["status"], fix["result"] = ("applied" if result.get("ok") else "failed"), result
        session_store.add_audit(session_id, target.device_id, fix["tool_id"], fix["args"], approved_by, result.get("ok"), result.get("summary"))
        session_store.update_session(session_id, status="fixing", report=report)
        events.publish(session_id, {"type": "fix", "index": index, "fix": fix})

        if result.get("ok"):
            await _verify(session, target, report, fix)
        else:
            _publish_status(session_id, "diagnosed", "Fix failed; see the result")
    await learn_from(session_id)
    return fix


async def _verify(session, target, report, fix):
    session_id = session["session_id"]
    _publish_status(session_id, "verifying", "Re-checking after the fix")
    steps = session_store.get_session(session_id)["steps"]
    tool_ids, evidence = [], []
    for step in steps:
        if step["kind"] == "tool" and step["tool_id"] not in ("performance_benchmark",) and (step["tool_id"], step["args"]) not in tool_ids:
            tool_ids.append((step["tool_id"], step["args"]))
    for tool_id, args in tool_ids[:4]:
        evidence.append((tool_id, args, await _run_step(session_id, target, tool_id, args or {}, kind="verify")))
    try:
        reply = await _chat([
            {"role": "system", "content": VERIFY_PROMPT.format(os_type=target.os_type)},
            {"role": "user", "content": f"Problem: {session['issue']}\nOriginal root cause: {report['root_cause']}\nFix applied: {fix['tool_id']} -> {fix['result'].get('summary')}\n\nFresh evidence:\n{_evidence_block(evidence, REPORT_EVIDENCE_CHARS)}"},
        ], json_mode=True)
        raw = _parse_json(reply["content"])
        verdict = {"resolved": bool(raw.get("resolved")), "explanation": str(raw.get("explanation", ""))[:1500], "next_steps": [str(item) for item in raw.get("next_steps", [])][:8], "after_fix": fix["tool_id"]}
    except LLMUnavailableError as error:
        verdict = {"resolved": None, "explanation": f"Fix applied, but the AI could not verify it: {error}", "next_steps": [], "after_fix": fix["tool_id"]}
    status = "resolved" if verdict["resolved"] else "diagnosed" if verdict["resolved"] is None else "unresolved"
    session_store.update_session(session_id, status=status, verify=verdict)
    events.publish(session_id, {"type": "verify", "verify": verdict})
    _publish_status(session_id, status, verdict["explanation"][:200])

"""The agentic diagnosis loop — the LLM "brain".

A diagnosis session runs in a background thread so the dashboard can poll it:

1. The problem text is spell-corrected and classified into playbook categories.
2. Similar past cases (decision memory) + the OS tool catalog go into the prompt.
3. The LLM calls READ tools; the server dispatches them to the agent, waits for
   the result, and feeds it back. This repeats up to MAX_STEPS.
4. The LLM returns a conclusion: root cause + ranked fixes. Fixes are CHANGE
   tools, which the loop records as proposals — they only run after the
   technician approves them in the dashboard.
5. If no LLM is available, the rule-based playbooks produce the same shape of
   result, so the tool always works.

The whole transcript (steps, tool results, proposal, outcome) lives in
SESSIONS keyed by session_id and is also the record saved to decision memory.
"""

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

from . import command_store, decision_memory, llm_provider, playbooks, tool_policy

logger = logging.getLogger(__name__)
MAX_STEPS = 8
TOOL_WAIT_SECONDS = 180

SESSIONS = {}
_lock = threading.Lock()

SYSTEM_PROMPT = (
    "You are Sentinel, an expert IT support technician diagnosing one laptop remotely. "
    "You work by calling diagnostic tools, reading their JSON results, and reasoning step by step. "
    "Rules:\n"
    "- Use READ tools freely to gather evidence; call one or a few at a time, then look at the results.\n"
    "- Base every conclusion on tool evidence you actually saw, not assumptions. Quote concrete numbers.\n"
    "- Tools marked NEEDS TECHNICIAN APPROVAL change the machine. Do NOT expect them to run; instead, when you "
    "have enough evidence, STOP calling tools and reply with your conclusion.\n"
    "- Prefer the safest effective fix. Mention risks (lost unsaved work, reboots, data).\n"
    "- The user may have typos; interpret intent generously.\n"
    "When you are done, reply with a plain-text final answer that includes: the root cause, the evidence, and a "
    "short numbered list of recommended fixes naming the exact tool for each. Do not call any more tools in that final message."
)


def get_session(session_id):
    with _lock:
        session = SESSIONS.get(session_id)
        return json.loads(json.dumps(session)) if session else None


def _update(session_id, **fields):
    with _lock:
        SESSIONS.setdefault(session_id, {}).update(fields)


def _append_step(session_id, step):
    with _lock:
        SESSIONS.setdefault(session_id, {}).setdefault("steps", []).append(step)


def start_session(device, problem, dispatch_tool, requested_by="technician"):
    """Create a session and run the loop in a background thread. Returns session_id."""
    session_id = f"diag-{uuid.uuid4().hex[:10]}"
    categories, corrected, corrections = playbooks.classify(problem)
    similar = decision_memory.search(corrected, limit=3, os_type=device.get("os_type"))
    _update(
        session_id,
        id=session_id, device_id=device.get("device_id"), hostname=device.get("hostname"), os_type=device.get("os_type"),
        problem=problem, corrected_problem=corrected, corrections=corrections, categories=categories,
        similar_cases=[{"id": case["id"], "symptom": case["symptom"], "root_cause": case.get("root_cause"),
                        "outcome": case.get("outcome"), "similarity": case.get("similarity")} for case in similar],
        status="running", mode=None, steps=[], findings=[], proposed_fixes=[], conclusion=None,
        started_at=datetime.now(timezone.utc).isoformat(), requested_by=requested_by,
    )
    thread = threading.Thread(target=_run, args=(session_id, device, dispatch_tool), daemon=True)
    thread.start()
    return session_id


def _run(session_id, device, dispatch_tool):
    try:
        if llm_provider.is_available():
            _update(session_id, mode="llm:" + llm_provider.provider_name())
            _run_llm(session_id, device, dispatch_tool)
        else:
            _update(session_id, mode="rules")
            _run_rules(session_id, device, dispatch_tool)
    except Exception as error:  # a crash must leave the session readable
        logger.exception("diagnosis session %s failed", session_id)
        _update(session_id, status="error", error=f"{type(error).__name__}: {error}")


def _collect_results(session_id):
    results = {}
    for step in get_session(session_id).get("steps", []):
        if step.get("kind") == "tool_result" and step.get("ok"):
            results[step["tool"]] = step["result"]
    return results


# ============================================================================
# LLM-driven loop
# ============================================================================

def _run_llm(session_id, device, dispatch_tool):
    session = get_session(session_id)
    tools = tool_policy.llm_tool_specs(device.get("os_type"), kinds=("read",))
    hints = ", ".join(playbooks.PLAYBOOKS[cat]["label"] for cat in session["categories"][:3])
    memory_text = _format_similar(session["similar_cases"])
    user = (
        f"Device: {device.get('hostname')} running {device.get('os_type')}.\n"
        f"Reported problem (verbatim): {session['problem']}\n"
        f"Spell-corrected: {session['corrected_problem']}\n"
        f"Likely area(s): {hints}.\n"
        f"{memory_text}\n"
        "Investigate with READ tools, then give your conclusion and recommended fixes."
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]

    for _ in range(MAX_STEPS):
        try:
            reply = llm_provider.chat(messages, tools=tools)
        except llm_provider.LLMUnavailableError:
            _append_step(session_id, {"kind": "note", "text": "LLM became unavailable; finishing with rule-based analysis."})
            return _run_rules(session_id, device, dispatch_tool, keep_results=True)

        if reply["tool_calls"]:
            messages.append({"role": "assistant", "content": reply["content"] or "",
                             "tool_calls": [{"id": call["id"], "type": "function",
                                             "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}
                                            for call in reply["tool_calls"]]})
            for call in reply["tool_calls"]:
                observation = _run_read_tool(session_id, device, dispatch_tool, call["name"], call["arguments"])
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                 "content": json.dumps(observation)[:6000]})
            continue

        # No tool calls -> final answer.
        _append_step(session_id, {"kind": "assistant", "text": reply["content"]})
        _finalize_llm(session_id, reply["content"])
        return

    _append_step(session_id, {"kind": "note", "text": f"Reached the {MAX_STEPS}-step limit; summarising."})
    _finalize_llm(session_id, None)


def _run_read_tool(session_id, device, dispatch_tool, tool_name, args):
    try:
        tool, clean, needs_approval = tool_policy.check(tool_name, args, device.get("os_type"))
    except tool_policy.ToolValidationError as error:
        _append_step(session_id, {"kind": "tool_result", "tool": tool_name, "ok": False, "error": str(error)})
        return {"error": str(error)}
    if needs_approval:
        # The model shouldn't call change tools; record it as a proposal instead of running it.
        _add_fix(session_id, tool_name, clean, "Proposed by the AI during investigation.")
        return {"note": f"{tool_name} needs technician approval; recorded as a proposed fix, not executed."}
    _append_step(session_id, {"kind": "tool_call", "tool": tool_name, "args": clean})
    outcome = dispatch_tool(device["device_id"], tool_name, tool["kind"], clean, session_id)
    ok = outcome.get("status") == "done"
    _append_step(session_id, {"kind": "tool_result", "tool": tool_name, "ok": ok,
                              "result": outcome.get("result"), "error": outcome.get("error")})
    return outcome.get("result") if ok else {"error": outcome.get("error", "tool failed")}


def _finalize_llm(session_id, final_text):
    results = _collect_results(session_id)
    rule_analysis = playbooks.analyze(results)
    for fix in rule_analysis["fixes"]:
        _add_fix(session_id, fix["tool"], fix["args"], fix["reason"])
    _update(session_id, findings=rule_analysis["findings"], status="awaiting_decision",
            conclusion=final_text or _summarize(rule_analysis["findings"]),
            finished_at=datetime.now(timezone.utc).isoformat())


# ============================================================================
# Rule-based loop (fallback, and when no LLM is configured)
# ============================================================================

def _run_rules(session_id, device, dispatch_tool, keep_results=False):
    session = get_session(session_id)
    plan = playbooks.plan_tools(session["categories"], session["corrected_problem"])
    for tool_name, args in plan:
        try:
            tool, clean, needs_approval = tool_policy.check(tool_name, args, device.get("os_type"))
        except tool_policy.ToolValidationError:
            continue
        if needs_approval:
            continue
        _append_step(session_id, {"kind": "tool_call", "tool": tool_name, "args": clean})
        outcome = dispatch_tool(device["device_id"], tool_name, tool["kind"], clean, session_id)
        _append_step(session_id, {"kind": "tool_result", "tool": tool_name, "ok": outcome.get("status") == "done",
                                  "result": outcome.get("result"), "error": outcome.get("error")})
    results = _collect_results(session_id)
    analysis = playbooks.analyze(results)
    for fix in analysis["fixes"]:
        _add_fix(session_id, fix["tool"], fix["args"], fix["reason"])
    conclusion = _summarize(analysis["findings"])
    memory = _format_similar(session["similar_cases"], short=True)
    if memory:
        conclusion += "\n\n" + memory
    _update(session_id, findings=analysis["findings"], status="awaiting_decision", conclusion=conclusion,
            finished_at=datetime.now(timezone.utc).isoformat())


def _summarize(findings):
    if not findings:
        return "No significant problems were found by the automated checks. If the issue persists, describe what the user sees and run a targeted check or a stress test."
    top = findings[0]
    lines = [f"Most likely cause: {top['title']} ({top['severity']}). {top['detail']}"]
    if len(findings) > 1:
        lines.append("Other findings: " + "; ".join(item["title"] for item in findings[1:5]))
    lines.append("Review the proposed fixes below and approve the ones you want to run.")
    return "\n".join(lines)


def _format_similar(cases, short=False):
    if not cases:
        return "" if short else "No similar past cases are on record yet."
    header = "Similar past cases from memory:" if not short else "From memory — similar past cases:"
    lines = [header]
    for case in cases:
        outcome = case.get("outcome") or "unknown"
        lines.append(f"- {case['symptom'][:70]} → {(case.get('root_cause') or 'n/a')[:80]} (outcome: {outcome})")
    return "\n".join(lines)


# ============================================================================
# Fixes (proposals) and outcome recording
# ============================================================================

def _add_fix(session_id, tool_name, args, reason):
    with _lock:
        session = SESSIONS.setdefault(session_id, {})
        fixes = session.setdefault("proposed_fixes", [])
        if any(fix["tool"] == tool_name and fix["args"] == args for fix in fixes):
            return
        tool = tool_policy.TOOLS.get(tool_name, {})
        fixes.append({"id": f"fix-{uuid.uuid4().hex[:8]}", "tool": tool_name, "title": tool.get("title", tool_name),
                      "kind": tool.get("kind", "change"), "args": args, "reason": reason, "status": "proposed",
                      "command_id": None})


def record_outcome(session_id, outcome, notes="", device=None):
    """Save the finished session to decision memory (outcome: fixed / not_fixed / partial)."""
    session = get_session(session_id)
    if not session:
        return None
    actions = []
    for step in session.get("steps", []):
        if step.get("kind") == "fix_executed":
            actions.append({"tool": step["tool"], "args": step.get("args", {}), "status": step.get("status")})
    root_cause = (session.get("conclusion") or "").split("\n")[0]
    case = decision_memory.save_case(
        symptom=session.get("corrected_problem") or session.get("problem"),
        category=(session.get("categories") or ["unknown"])[0],
        root_cause=root_cause,
        findings=session.get("findings", []),
        actions=actions,
        outcome=outcome,
        notes=notes,
        device=device or {"device_id": session.get("device_id"), "hostname": session.get("hostname"), "os_type": session.get("os_type")},
        session_id=session_id,
    )
    _update(session_id, status="closed", outcome=outcome, case_id=case["id"])
    return case


def mark_fix_executed(session_id, fix_id, command_id, status, args=None, tool=None):
    with _lock:
        session = SESSIONS.get(session_id)
        if not session:
            return
        for fix in session.get("proposed_fixes", []):
            if fix["id"] == fix_id:
                fix["status"] = status
                fix["command_id"] = command_id
                tool, args = fix["tool"], fix["args"]
        session.setdefault("steps", []).append({"kind": "fix_executed", "tool": tool, "args": args or {},
                                                "status": status, "command_id": command_id})

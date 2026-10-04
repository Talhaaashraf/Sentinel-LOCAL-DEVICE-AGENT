"""API routes for the agentic diagnosis, command/approval queue, install scripts,
decision memory, health score, stress tests and reports.

Mounted by backend.main via include_router(router). The agent-facing endpoints
(/api/agents/commands/...) authenticate with the agent bearer token; the rest sit
behind the dashboard session like the other /api routes.
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from . import agent_loop, command_store, decision_memory, health_score, llm_provider, metrics_history, playbooks, schedules, tool_policy
from .device_store import get_device_by_token, get_latest_report, list_devices
from .dispatch import dispatch_read_tool
from .rules_engine import health_from_report

logger = logging.getLogger(__name__)
router = APIRouter()

# Tools safe to run across many machines at once without per-machine review.
FLEET_ALLOWED_TOOLS = {"clean_junk", "install_updates", "create_restore_point",
                       "security_status", "pending_updates", "system_profile", "disk_usage"}


def _device_or_404(device_id):
    for device in list_devices():
        if device["device_id"] == device_id:
            return device
    raise HTTPException(status_code=404, detail="Device not found")


def _authenticated_device(request: Request):
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer agent token required")
    device = get_device_by_token(authorization[7:].strip())
    if not device:
        raise HTTPException(status_code=401, detail="Invalid agent token")
    return device


# ============================================================================
# Tool catalog + LLM status
# ============================================================================

@router.get("/api/tools")
def tools(os_type: str = ""):
    return {"kinds": tool_policy.CATALOG["kinds"], "tools": tool_policy.catalog_for(os_type or None),
            "shell_enabled": tool_policy.shell_allowed()}


@router.get("/api/llm/status")
def llm_status():
    return llm_provider.status(force=True)


@router.get("/api/presets")
def presets():
    return {"presets": playbooks.presets()}


# ============================================================================
# Diagnosis sessions (the agentic loop)
# ============================================================================

@router.post("/api/diagnose/start")
def diagnose_start(payload: dict):
    device = _device_or_404(str(payload.get("device_id", "")))
    problem = str(payload.get("problem", "")).strip()
    preset = str(payload.get("preset", "")).strip()
    if preset and preset in playbooks.PLAYBOOKS and not problem:
        problem = playbooks.PLAYBOOKS[preset]["label"]
    if not problem:
        raise HTTPException(status_code=400, detail="Describe the problem or choose a preset")
    session_id = agent_loop.start_session(device, problem, dispatch_read_tool)
    return {"session_id": session_id}


@router.get("/api/diagnose/{session_id}")
def diagnose_get(session_id: str):
    session = agent_loop.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.post("/api/diagnose/{session_id}/approve")
def diagnose_approve(session_id: str, payload: dict):
    """Approve one proposed fix -> create the change command (pending_approval cleared)."""
    session = agent_loop.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    fix = next((item for item in session.get("proposed_fixes", []) if item["id"] == payload.get("fix_id")), None)
    if not fix:
        raise HTTPException(status_code=404, detail="Proposed fix not found")
    device = _device_or_404(session["device_id"])
    try:
        tool, clean, _ = tool_policy.check(fix["tool"], fix["args"], device.get("os_type"))
    except tool_policy.ToolValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    # Approved by the technician here -> queue directly (needs_approval=False because this IS the approval).
    command = command_store.create_command(
        device_id=device["device_id"], tool=tool["name"], kind=tool["kind"], args=clean,
        requested_by="technician", needs_approval=False, reason=fix.get("reason"), session_id=session_id,
    )
    agent_loop.mark_fix_executed(session_id, fix["id"], command["id"], "running")
    return {"command_id": command["id"], "status": command["status"]}


@router.post("/api/diagnose/{session_id}/outcome")
def diagnose_outcome(session_id: str, payload: dict):
    outcome = str(payload.get("outcome", "")).strip()
    if outcome not in ("fixed", "not_fixed", "partial"):
        raise HTTPException(status_code=400, detail="outcome must be fixed, not_fixed or partial")
    case = agent_loop.record_outcome(session_id, outcome, str(payload.get("notes", "")))
    if not case:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"saved": True, "case_id": case["id"]}


# ============================================================================
# Direct command queue + approvals (used by the Live/Apps tabs and stress tests)
# ============================================================================

@router.post("/api/devices/{device_id}/commands")
def create_command(device_id: str, payload: dict):
    device = _device_or_404(device_id)
    try:
        tool, clean, needs_approval = tool_policy.check(str(payload.get("tool", "")), payload.get("args") or {}, device.get("os_type"))
    except tool_policy.ToolValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    # If the caller explicitly pre-approves (approve=true), skip the pending state.
    pre_approved = bool(payload.get("approve"))
    command = command_store.create_command(
        device_id=device_id, tool=tool["name"], kind=tool["kind"], args=clean,
        requested_by=str(payload.get("requested_by", "technician")),
        needs_approval=needs_approval and not pre_approved, reason=str(payload.get("reason", "")) or None,
        session_id=payload.get("session_id"),
    )
    return command


@router.get("/api/devices/{device_id}/commands")
def device_commands(device_id: str, limit: int = 50):
    return {"commands": command_store.list_commands(device_id=device_id, limit=min(limit, 200))}


@router.get("/api/commands/{command_id}")
def command_detail(command_id: str):
    command = command_store.get_command(command_id)
    if not command:
        raise HTTPException(status_code=404, detail="Command not found")
    return command


@router.post("/api/commands/{command_id}/approve")
def approve_command(command_id: str):
    if not command_store.approve(command_id):
        raise HTTPException(status_code=409, detail="Command is not awaiting approval")
    return {"approved": True}


@router.post("/api/commands/{command_id}/deny")
def deny_command(command_id: str):
    if not command_store.deny(command_id):
        raise HTTPException(status_code=409, detail="Command is not awaiting approval")
    return {"denied": True}


@router.post("/api/commands/{command_id}/cancel")
def cancel_command(command_id: str):
    result = command_store.cancel(command_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Command not found")
    if result is False:
        raise HTTPException(status_code=409, detail="Command already finished")
    return {"cancelled": True}


@router.get("/api/devices/{device_id}/pending-approvals")
def pending_approvals(device_id: str):
    return {"commands": command_store.list_commands(device_id=device_id, status="pending_approval", limit=100)}


# ============================================================================
# Agent-facing command channel
# ============================================================================

@router.get("/api/agents/commands/next")
def agent_next_commands(request: Request):
    device = _authenticated_device(request)
    return {"commands": command_store.claim_next(device["device_id"])}


@router.post("/api/agents/commands/{command_id}/progress")
async def agent_command_progress(command_id: str, request: Request):
    device = _authenticated_device(request)
    payload = await request.json()
    command_store.record_progress(command_id, device["device_id"], payload.get("progress"))
    return {"ok": True}


@router.post("/api/agents/commands/{command_id}/result")
async def agent_command_result(command_id: str, request: Request):
    device = _authenticated_device(request)
    payload = await request.json()
    result = payload.get("result")
    if payload.get("status") == "done":
        result = _post_process(device["device_id"], command_id, result)
    command_store.record_result(command_id, device["device_id"], payload.get("status", "error"),
                                result=result, error=payload.get("error"))
    return {"ok": True}


def _post_process(device_id, command_id, result):
    """Compute storage growth, and log stress runs to history."""
    command = command_store.get_command(command_id)
    if not command:
        return result
    if command["tool"] == "storage_snapshot" and isinstance(result, dict):
        previous = command_store.latest_result(device_id, "storage_snapshot", before_id=command_id)
        if previous and previous.get("result"):
            result["growth"] = _storage_growth(previous["result"], result)
    if command["tool"] in ("stress_cpu", "stress_ram", "stress_disk", "network_speed") and isinstance(result, dict):
        try:
            from . import metrics_history
            metrics_history.record_stress(device_id, command["tool"], result)
        except Exception:
            logger.exception("stress history record failed")
    return result


def _storage_growth(previous, current):
    old = {name: info.get("bytes", 0) for name, info in (previous.get("folders") or {}).items()}
    growth = []
    for name, info in (current.get("folders") or {}).items():
        delta = info.get("bytes", 0) - old.get(name, info.get("bytes", 0))
        if delta > 0:
            growth.append({"name": name, "path": info.get("path"), "grew_bytes": delta,
                           "grew": _human(delta), "since": previous.get("captured_at")})
    growth.sort(key=lambda item: item["grew_bytes"], reverse=True)
    return growth


def _human(value):
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ============================================================================
# Network speed-test endpoints (the agent measures against these)
# ============================================================================

@router.get("/api/agents/speedtest/download")
def speedtest_download(request: Request, size_mb: int = 50):
    _authenticated_device(request)
    size_mb = max(1, min(size_mb, 500))
    chunk = b"0" * (1024 * 1024)

    def generate():
        for _ in range(size_mb):
            yield chunk

    return StreamingResponse(generate(), media_type="application/octet-stream")


@router.post("/api/agents/speedtest/upload")
async def speedtest_upload(request: Request):
    _authenticated_device(request)
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
    return {"received_bytes": total}


# ============================================================================
# Software / Startup / Backups tabs (synchronous read via the agent)
# ============================================================================

def _run_read(device_id, tool, args):
    device = _device_or_404(device_id)
    if device["status"] != "online":
        raise HTTPException(status_code=409, detail="Device is offline")
    outcome = dispatch_read_tool(device_id, tool, "read", args, session_id=None, timeout=120)
    if outcome["status"] != "done":
        raise HTTPException(status_code=504, detail=outcome.get("error", "The agent did not respond"))
    return outcome["result"]


@router.get("/api/devices/{device_id}/software")
def device_software(device_id: str, search: str = ""):
    return _run_read(device_id, "installed_apps", {"include_store_apps": True, "search": search, "limit": 1000})


@router.get("/api/devices/{device_id}/startup")
def device_startup(device_id: str):
    return _run_read(device_id, "startup_items", {})


@router.get("/api/devices/{device_id}/backups")
def device_backups(device_id: str):
    return _run_read(device_id, "list_backups", {})


# ============================================================================
# Scheduled checks
# ============================================================================

@router.get("/api/schedules")
def list_schedules(device_id: str = ""):
    return {"schedules": schedules.list_all(device_id or None)}


@router.post("/api/schedules")
def create_schedule(payload: dict):
    device = _device_or_404(str(payload.get("device_id", "")))
    preset = str(payload.get("preset", "")).strip()
    if preset not in playbooks.PLAYBOOKS:
        raise HTTPException(status_code=400, detail="Unknown preset")
    interval = int(payload.get("interval_minutes", 60) or 60)
    return schedules.create(device["device_id"], preset, interval)


@router.post("/api/schedules/{schedule_id}/toggle")
def toggle_schedule(schedule_id: str, payload: dict):
    schedule = schedules.set_enabled(schedule_id, bool(payload.get("enabled", True)))
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return schedule


@router.delete("/api/schedules/{schedule_id}")
def delete_schedule(schedule_id: str):
    if not schedules.delete(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"deleted": True}


# ============================================================================
# Fleet bulk actions
# ============================================================================

@router.post("/api/fleet/commands")
def fleet_commands(payload: dict):
    tool_name = str(payload.get("tool", ""))
    if tool_name not in FLEET_ALLOWED_TOOLS:
        raise HTTPException(status_code=400, detail=f"{tool_name} is not allowed for fleet actions")
    device_ids = payload.get("device_ids") or []
    if not isinstance(device_ids, list) or not device_ids:
        raise HTTPException(status_code=400, detail="device_ids must be a non-empty list")
    by_id = {device["device_id"]: device for device in list_devices()}
    created, skipped = [], []
    for device_id in device_ids:
        device = by_id.get(device_id)
        if not device:
            skipped.append({"device_id": device_id, "reason": "not found"})
            continue
        try:
            tool, clean, _ = tool_policy.check(tool_name, payload.get("args") or {}, device.get("os_type"))
        except tool_policy.ToolValidationError as error:
            skipped.append({"device_id": device_id, "reason": str(error)})
            continue
        command = command_store.create_command(
            device_id=device_id, tool=tool["name"], kind=tool["kind"], args=clean,
            requested_by="technician (fleet)", needs_approval=False, reason="Fleet action",
        )
        created.append({"device_id": device_id, "nickname": device.get("nickname"), "command_id": command["id"]})
    return {"created": created, "skipped": skipped}


# ============================================================================
# History & trends
# ============================================================================

@router.get("/api/devices/{device_id}/history")
def device_history(device_id: str, hours: int = 24):
    hours = max(1, min(hours, 24 * metrics_history.RETENTION_DAYS))
    return metrics_history.history(device_id, hours=hours)


@router.get("/api/devices/{device_id}/stress-history")
def device_stress_history(device_id: str, limit: int = 50):
    return {"runs": metrics_history.stress_runs(device_id, limit=min(limit, 200))}


# ============================================================================
# Health score
# ============================================================================

@router.get("/api/devices/{device_id}/health-score")
def device_health_score(device_id: str):
    from .alert_store import get_alerts
    latest = get_latest_report(device_id)
    if not latest:
        raise HTTPException(status_code=404, detail="No report for this device yet")
    return health_score.score(latest["diagnostics"], get_alerts(20, device_id))


# ============================================================================
# Decision memory
# ============================================================================

@router.get("/api/decisions/cases")
def decision_cases(limit: int = 100):
    return {"cases": decision_memory.list_cases(limit=min(limit, 500))}


@router.get("/api/decisions/cases/{case_id}")
def decision_case(case_id: str):
    case = decision_memory.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@router.get("/api/decisions/adrs")
def decision_adrs():
    return {"adrs": decision_memory.adr_documents()}


# ============================================================================
# Report export
# ============================================================================

@router.get("/api/devices/{device_id}/report", response_class=HTMLResponse)
def device_report(device_id: str):
    device = _device_or_404(device_id)
    latest = get_latest_report(device_id)
    diagnostics = latest["diagnostics"] if latest else {}
    from .alert_store import get_alerts
    score = health_score.score(diagnostics, get_alerts(20, device_id)) if latest else {"score": "?", "grade": "unknown", "deductions": []}
    commands = command_store.list_commands(device_id=device_id, limit=30)
    changes = [command for command in commands if command["kind"] in ("change", "stress") and command["status"] in ("done", "cancelled")]
    return HTMLResponse(_render_report(device, diagnostics, score, changes))


def _render_report(device, diagnostics, score, changes):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = "".join(
        f"<tr><td>{command['tool']}</td><td>{command['status']}</td><td>{command.get('finished_at', '')}</td></tr>"
        for command in changes
    ) or "<tr><td colspan='3'>No changes were made.</td></tr>"
    cpu = diagnostics.get("cpu", {}).get("total_usage_percent", "?")
    ram = diagnostics.get("memory", {}).get("ram", {}).get("usage_percent", "?")
    deductions = "".join(f"<li>{item['reason']} (−{item['points']})</li>" for item in score["deductions"]) or "<li>No deductions.</li>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Report — {device['hostname']}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;max-width:800px;margin:30px auto;color:#1b2b2f}}
h1{{margin-bottom:0}}table{{border-collapse:collapse;width:100%;margin:12px 0}}td,th{{border:1px solid #ccd;padding:8px;text-align:left}}
.score{{font-size:2.5rem;font-weight:bold}}.good{{color:#1a7f54}}.fair{{color:#b8860b}}.poor{{color:#c0392b}}
@media print{{a{{display:none}}}}</style></head><body>
<h1>Device health report</h1><p>{device['hostname']} · {device['os_type']} · generated {now}</p>
<p>Health score: <span class="score {score['grade']}">{score['score']}</span> / 100 ({score['grade']})</p>
<ul>{deductions}</ul>
<h2>Snapshot</h2><table><tr><th>CPU</th><td>{cpu}%</td></tr><tr><th>RAM</th><td>{ram}%</td></tr></table>
<h2>Changes made</h2><table><tr><th>Action</th><th>Result</th><th>When</th></tr>{rows}</table>
<p><a href="#" onclick="window.print()">Print / Save as PDF</a></p>
</body></html>"""

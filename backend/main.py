"""FastAPI server for a local, Wazuh-inspired device health monitor."""

import asyncio
import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .actions import run_action
from .alert_store import add_alert, get_alerts
from .collectors import collect_report
from .device_store import create_pending_token, delete_device, get_device_by_token, get_latest_report, list_devices, mark_offline_devices, register_device, save_report
from .groq_agent import AIModelUnavailableError, AIUnavailableError, chat, diagnose, is_ai_available
from .rules_engine import evaluate_rules, evaluate_security_rules, health_from_report
from .security_checks import collect_security_report

app = FastAPI(title="Device Health Monitor")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
logger = logging.getLogger(__name__)
connections = set()
previous_report = None
previous_reports = {}
chat_sessions = {}
latest_security_report = None
SECURITY_REFRESH_SECONDS = 300


def require_admin(request):
    configured = os.getenv("ADMIN_API_KEY", "").strip()
    if not configured or not secrets.compare_digest(request.headers.get("X-Admin-Key", ""), configured):
        raise HTTPException(status_code=401, detail="Admin authentication required")


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    logger.exception("Unhandled server error for %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def snapshot_with_alerts(report):
    global previous_report
    for alert in evaluate_rules(report, previous_report):
        add_alert(alert, "local-server")
    previous_report = report
    return report


@app.on_event("startup")
async def start_monitor():
    logger.info("Registered FastAPI routes:")
    for route in app.routes:
        methods = ",".join(sorted(getattr(route, "methods", set()) or []))
        route_line = f"  {methods or 'WEBSOCKET'} {route.path}"
        logger.info(route_line)
        print(route_line, flush=True)
    asyncio.create_task(monitor_loop())
    asyncio.create_task(offline_loop())
    asyncio.create_task(security_loop())


async def refresh_security():
    global latest_security_report
    report = await asyncio.to_thread(collect_security_report)
    for alert in evaluate_security_rules(report):
        add_alert(alert, "local-server")
    latest_security_report = report
    return report


async def security_loop():
    while True:
        try:
            await refresh_security()
        except Exception:
            logger.exception("Security check failed")
        await asyncio.sleep(SECURITY_REFRESH_SECONDS)


async def monitor_loop():
    while True:
        try:
            report = snapshot_with_alerts(collect_report())
            for connection in list(connections):
                try:
                    await connection.send_json(report)
                except Exception:
                    connections.discard(connection)
        except Exception:
            pass
        await asyncio.sleep(5)


async def offline_loop():
    while True:
        try:
            mark_offline_devices()
        except Exception:
            logger.exception("Failed to update remote device statuses")
        await asyncio.sleep(10)


@app.get("/", include_in_schema=False)
def frontend():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/diagnostics")
def diagnostics():
    return snapshot_with_alerts(collect_report())


@app.get("/api/diagnostics/health")
def health():
    return health_from_report(collect_report())


@app.websocket("/ws/monitor")
async def monitor(websocket: WebSocket):
    await websocket.accept()
    connections.add(websocket)
    try:
        await websocket.send_json(snapshot_with_alerts(collect_report()))
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, Exception):
        connections.discard(websocket)


@app.get("/api/diagnostics/security")
async def security():
    if latest_security_report is None:
        return await refresh_security()
    return latest_security_report


@app.get("/api/alerts")
def alerts(limit: int = 100):
    return {"alerts": get_alerts(max(1, min(limit, 500)), "local-server")}


@app.post("/api/agents/generate-token")
def generate_agent_token(request: Request):
    require_admin(request)
    token, expires_at = create_pending_token()
    return {"token": token, "expires_at": expires_at, "message": "Use this token once when registering a new agent."}


@app.post("/api/agents/register")
def register_agent(payload: dict):
    token = str(payload.get("token", "")).strip()
    hostname = str(payload.get("hostname", "Unknown")).strip()
    if not token or not hostname:
        raise HTTPException(status_code=400, detail="token and hostname are required")
    try:
        return register_device(token, hostname, str(payload.get("os_type", "Unknown")), str(payload.get("nickname", "")), payload.get("device_id"))
    except ValueError as error:
        raise HTTPException(status_code=401, detail="Invalid or expired agent token") from error


def authenticated_device(request: Request):
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer agent token required")
    device = get_device_by_token(authorization[7:].strip())
    if not device:
        raise HTTPException(status_code=401, detail="Invalid agent token")
    return device


@app.post("/api/agents/report")
def agent_report(payload: dict, request: Request):
    device = authenticated_device(request)
    diagnostics_payload = payload.get("diagnostics")
    if not isinstance(diagnostics_payload, dict):
        raise HTTPException(status_code=400, detail="diagnostics object is required")
    device_id = device["device_id"]
    previous = previous_reports.get(device_id)
    alerts_for_report = evaluate_rules(diagnostics_payload, previous) + evaluate_security_rules(diagnostics_payload.get("security"))
    for alert in alerts_for_report:
        add_alert(alert, device_id)
    previous_reports[device_id] = diagnostics_payload
    save_report(device_id, str(payload.get("timestamp") or __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()), diagnostics_payload, payload.get("spike_detected", False))
    return {"accepted": True, "device_id": device_id, "alerts_created": len(alerts_for_report)}


@app.get("/api/agents")
def agents():
    result = []
    for device in list_devices():
        latest = get_latest_report(device["device_id"])
        health_result = health_from_report(latest["diagnostics"]) if latest else {"status": "unknown", "message": "No report received yet.", "issues": []}
        result.append({**device, "health": health_result})
    return {"agents": result}


@app.get("/api/agents/{device_id}/diagnostics")
def agent_diagnostics(device_id: str):
    latest = get_latest_report(device_id)
    if not latest:
        raise HTTPException(status_code=404, detail="No diagnostics found for device")
    return latest


@app.get("/api/agents/{device_id}/security")
def agent_security(device_id: str):
    latest = get_latest_report(device_id)
    if not latest:
        raise HTTPException(status_code=404, detail="No diagnostics found for device")
    security_data = latest["diagnostics"].get("security")
    if security_data is None:
        raise HTTPException(status_code=404, detail="This device has not reported security data yet")
    return security_data


@app.get("/api/agents/{device_id}/alerts")
def agent_alerts(device_id: str, limit: int = 100):
    return {"alerts": get_alerts(max(1, min(limit, 500)), device_id)}


@app.delete("/api/agents/{device_id}")
def revoke_agent(device_id: str, request: Request):
    require_admin(request)
    if not delete_device(device_id):
        raise HTTPException(status_code=404, detail="Device not found")
    return {"deleted": True, "device_id": device_id}


@app.post("/api/agents/{device_id}/agent-diagnose")
def diagnose_device(device_id: str):
    if not is_ai_available():
        raise HTTPException(status_code=503, detail="AI technician unavailable: configure GROQ_API_KEY in .env")
    latest = get_latest_report(device_id)
    if not latest:
        raise HTTPException(status_code=404, detail="No diagnostics found for device")
    try:
        return diagnose(latest["diagnostics"], get_alerts(20, device_id))
    except (AIModelUnavailableError, AIUnavailableError) as error:
        raise HTTPException(status_code=503, detail="AI technician is temporarily unavailable") from error


@app.get("/api/agents/download/{platform}")
def download_agent(platform: str):
    safe_platform = platform.lower()
    if safe_platform not in {"windows", "mac", "linux"}:
        raise HTTPException(status_code=404, detail="Platform must be windows, mac, or linux")
    builds = Path(__file__).resolve().parent.parent / "agent_builds"
    candidates = {"windows": ("DeviceHealthAgent.exe", "application/vnd.microsoft.portable-executable"), "mac": ("DeviceHealthAgent", "application/octet-stream"), "linux": ("DeviceHealthAgent", "application/octet-stream")}
    folder = "macos" if safe_platform == "mac" else safe_platform
    filename, media_type = candidates[safe_platform]
    root = os.path.realpath(builds)
    path = os.path.realpath(builds / folder / filename)
    if os.path.commonpath([root, path]) != root:
        raise HTTPException(status_code=400, detail="Invalid download path")
    if not path.exists():
        messages = {"windows": "Windows installer not built yet. See agent/README.md", "mac": "macOS installer not built yet. See agent/README.md", "linux": "Linux installer not built yet. See agent/README.md"}
        return JSONResponse(status_code=404, content={"available": False, "message": messages[safe_platform], "manual_install": "pip install -r agent_requirements.txt && python monitor_agent.py"})
    return FileResponse(path, filename=filename, media_type=media_type)


@app.post("/api/agent/diagnose")
def agent_diagnose(payload: dict):
    if not is_ai_available():
        raise HTTPException(status_code=503, detail="AI technician unavailable: configure GROQ_API_KEY in .env")
    try:
        return diagnose(payload.get("diagnostics") or payload.get("report") or collect_report(), payload.get("alert_history") or get_alerts(20))
    except AIModelUnavailableError as error:
        raise HTTPException(status_code=503, detail="AI technician is temporarily unavailable") from error
    except AIUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/agent/action")
def agent_action(payload: dict):
    try:
        return run_action(payload.get("action_id"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/agent/chat")
def chat_status():
    available = is_ai_available()
    return {"available": available, "message": "AI technician ready" if available else "AI technician unavailable"}


@app.post("/api/agent/chat")
def agent_chat(payload: dict):
    if not is_ai_available():
        raise HTTPException(status_code=503, detail="AI technician unavailable: configure GROQ_API_KEY in .env")
    message = str(payload.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    session_id = str(payload.get("session_id", "default"))
    history = chat_sessions.setdefault(session_id, [])
    try:
        reply = chat(message, payload.get("diagnostics") or collect_report(), history)
        history.extend([{"role": "user", "content": message}, {"role": "assistant", "content": reply.get("plain_explanation", "")}])
        chat_sessions[session_id] = history[-6:]
        return {"reply": reply, "session_id": session_id}
    except AIModelUnavailableError as error:
        raise HTTPException(status_code=503, detail="AI technician is temporarily unavailable") from error
    except AIUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

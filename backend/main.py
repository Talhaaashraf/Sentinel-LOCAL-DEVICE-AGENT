"""FastAPI server for a local, Wazuh-inspired, AI-powered device health monitor."""

import asyncio
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .actions import run_action
from .alert_store import add_alert, get_alerts
from .auth import PUBLIC_PATH_PREFIXES, SESSION_COOKIE_NAME, auth_enabled, check_password, has_valid_session, session_token
from .collectors import collect_report
from .device_store import create_pending_token, delete_device, get_device_by_token, get_latest_report, list_devices, mark_offline_devices, register_device, save_report
from .event_logs import collect_event_log_report
from .groq_agent import AIModelUnavailableError, AIUnavailableError, chat, diagnose, diagnose_logs, is_ai_available
from . import llm_provider
from .diagnostics_api import router as diagnostics_router
from .install_api import router as install_router
from .performance_checks import collect_performance_report
from .rules_engine import evaluate_event_log_rules, evaluate_performance_rules, evaluate_rules, evaluate_security_rules, health_from_report
from .security_checks import collect_security_report

app = FastAPI(title="Sentinel Device Health Monitor")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
app.include_router(diagnostics_router)
app.include_router(install_router)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

logger = logging.getLogger(__name__)

# In-memory state for the local-server monitor loop and its dashboard-facing caches.
connections = set()
previous_report = None
previous_reports = {}
chat_sessions = {}
latest_security_report = None
latest_performance_report = None
latest_event_log_report = None

SECURITY_REFRESH_SECONDS = 300
EVENT_LOG_REFRESH_SECONDS = 600


# ============================================================================
# Auth
# ============================================================================

def require_admin(request):
    """Stricter, separate gate for provisioning actions (generate/revoke agent tokens)."""
    configured = os.getenv("ADMIN_API_KEY", "").strip()
    if not configured or not secrets.compare_digest(request.headers.get("X-Admin-Key", ""), configured):
        raise HTTPException(status_code=401, detail="Admin authentication required")


@app.middleware("http")
async def dashboard_auth_middleware(request: Request, call_next):
    path = request.url.path
    if not auth_enabled() or path.startswith(PUBLIC_PATH_PREFIXES):
        return await call_next(request)
    if has_valid_session(request.cookies):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "Login required"})
    return RedirectResponse(url="/login")


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(FRONTEND_DIR / "login.html")


@app.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    form = await request.form()
    if not check_password(str(form.get("password", ""))):
        return RedirectResponse(url="/login?error=1", status_code=303)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(SESSION_COOKIE_NAME, session_token(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return response


@app.get("/logout", include_in_schema=False)
def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ============================================================================
# Startup and background loops
# ============================================================================

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
    if auth_enabled():
        logger.info("Dashboard login is enabled (DASHBOARD_PASSWORD/ADMIN_API_KEY set).")
    else:
        logger.warning("Dashboard login is disabled: set DASHBOARD_PASSWORD or ADMIN_API_KEY in .env to require sign-in.")
    asyncio.create_task(monitor_loop())
    asyncio.create_task(offline_loop())
    asyncio.create_task(security_loop())
    asyncio.create_task(event_log_loop())


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


async def refresh_performance():
    """Run the active benchmark suite. Called on demand only (see /api/diagnostics/performance);
    unlike security or event-log checks it briefly writes to disk and uses CPU, so it never runs
    on a timer here."""
    global latest_performance_report
    previous = latest_performance_report
    report = await asyncio.to_thread(collect_performance_report)
    for alert in evaluate_performance_rules(report, previous):
        add_alert(alert, "local-server")
    latest_performance_report = report
    return report


async def refresh_event_logs():
    global latest_event_log_report
    report = await asyncio.to_thread(collect_event_log_report)
    for alert in evaluate_event_log_rules(report):
        add_alert(alert, "local-server")
    latest_event_log_report = report
    return report


async def event_log_loop():
    while True:
        try:
            await refresh_event_logs()
        except Exception:
            logger.exception("Event log check failed")
        await asyncio.sleep(EVENT_LOG_REFRESH_SECONDS)


# ============================================================================
# Local device diagnostics
# ============================================================================

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
    if not has_valid_session(websocket.cookies):
        await websocket.close(code=4401)
        return
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


@app.get("/api/diagnostics/performance")
def performance():
    if latest_performance_report is None:
        return JSONResponse(status_code=404, content={"detail": "No performance test has run yet. POST to this endpoint to run one."})
    return latest_performance_report


@app.post("/api/diagnostics/performance")
async def run_performance():
    return await refresh_performance()


@app.get("/api/diagnostics/logs")
async def event_logs_endpoint():
    if latest_event_log_report is None:
        return await refresh_event_logs()
    return latest_event_log_report


@app.post("/api/diagnostics/logs/diagnose")
def diagnose_event_logs_endpoint():
    if not is_ai_available():
        raise HTTPException(status_code=503, detail="AI technician unavailable: configure GROQ_API_KEY in .env")
    if latest_event_log_report is None:
        raise HTTPException(status_code=404, detail="No event log data collected yet")
    try:
        return diagnose_logs(latest_event_log_report)
    except (AIModelUnavailableError, AIUnavailableError) as error:
        raise HTTPException(status_code=503, detail="AI technician is temporarily unavailable") from error


@app.get("/api/alerts")
def alerts(limit: int = 100):
    return {"alerts": get_alerts(max(1, min(limit, 500)), "local-server")}


# ============================================================================
# Remote agent provisioning and reporting
# ============================================================================

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
    previous = previous_reports.get(device_id) or {}
    alerts_for_report = (
        evaluate_rules(diagnostics_payload, previous)
        + evaluate_security_rules(diagnostics_payload.get("security"))
        + evaluate_performance_rules(diagnostics_payload.get("performance"), previous.get("performance"))
        + evaluate_event_log_rules(diagnostics_payload.get("event_logs"))
    )
    for alert in alerts_for_report:
        add_alert(alert, device_id)
    previous_reports[device_id] = diagnostics_payload

    timestamp = str(payload.get("timestamp") or datetime.now(timezone.utc).isoformat())
    save_report(device_id, timestamp, diagnostics_payload, payload.get("spike_detected", False))
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


def _device_diagnostics_field(device_id, field, missing_message):
    latest = get_latest_report(device_id)
    if not latest:
        raise HTTPException(status_code=404, detail="No diagnostics found for device")
    value = latest["diagnostics"].get(field)
    if value is None:
        raise HTTPException(status_code=404, detail=missing_message)
    return value


@app.get("/api/agents/{device_id}/security")
def agent_security(device_id: str):
    return _device_diagnostics_field(device_id, "security", "This device has not reported security data yet")


@app.get("/api/agents/{device_id}/performance")
def agent_performance(device_id: str):
    return _device_diagnostics_field(device_id, "performance", "This device has not reported performance data yet")


@app.get("/api/agents/{device_id}/logs")
def agent_logs(device_id: str):
    return _device_diagnostics_field(device_id, "event_logs", "This device has not reported event log data yet")


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
    candidates = {
        "windows": ("DeviceHealthAgent.exe", "application/vnd.microsoft.portable-executable"),
        "mac": ("DeviceHealthAgent", "application/octet-stream"),
        "linux": ("DeviceHealthAgent", "application/octet-stream"),
    }
    folder = "macos" if safe_platform == "mac" else safe_platform
    filename, media_type = candidates[safe_platform]

    root = os.path.realpath(builds)
    path = os.path.realpath(builds / folder / filename)
    if os.path.commonpath([root, path]) != root:
        raise HTTPException(status_code=400, detail="Invalid download path")

    if not path.exists():
        messages = {
            "windows": "Windows installer not built yet. See agent/README.md",
            "mac": "macOS installer not built yet. See agent/README.md",
            "linux": "Linux installer not built yet. See agent/README.md",
        }
        return JSONResponse(status_code=404, content={
            "available": False,
            "message": messages[safe_platform],
            "manual_install": "pip install -r agent_requirements.txt && python monitor_agent.py",
        })
    return FileResponse(path, filename=filename, media_type=media_type)


# ============================================================================
# AI technician
# ============================================================================

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
    info = llm_provider.status()
    return {"available": info["available"], "provider": info["provider"], "model": info["model"],
            "message": info["detail"]}


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

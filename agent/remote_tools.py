"""Dispatcher that runs a catalog tool on the agent's machine.

The server sends {tool, args, run_id}; run_tool() validates against the shared
catalog, refuses anything not allowed on this platform (or raw shell unless the
agent was started with --allow-shell), and calls the matching implementation.
Platform-specific tools come from tools_windows or tools_unix; the rest from
tools_common and tools_stress. network_check, security_status, event_log_summary
and system_profile compose results from existing Sentinel collectors.
"""

import socket
import time

import tools_common as common
import tools_stress as stress
from tool_schema import current_platform, load_catalog, needs_approval, tools_by_name, validate_args, ToolValidationError

if common.IS_WINDOWS:
    import tools_windows as native
else:
    import tools_unix as native

try:
    from security_checks import collect_security_report
    from event_logs import collect_event_log_report
except ImportError:
    from .security_checks import collect_security_report
    from .event_logs import collect_event_log_report


class ToolContext:
    """Passed to long-running tools so they can stream progress and be cancelled."""

    def __init__(self, run_id, backup_dir, emit=None, is_cancelled=None):
        self.run_id = run_id
        self.backup_dir = backup_dir
        self._emit = emit
        self._is_cancelled = is_cancelled

    def progress(self, data):
        if self._emit:
            try:
                self._emit({"run_id": self.run_id, "progress": data})
            except Exception:
                pass
        return bool(self._is_cancelled and self._is_cancelled(self.run_id))


def _system_profile(**_):
    profile = common.system_basics()
    profile["pending_reboot"] = _pending_reboot()
    if common.IS_WINDOWS:
        rows, _ = common.ps_json("Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model", timeout=30)
        bios, _ = common.ps_json("Get-CimInstance Win32_BIOS | Select-Object SerialNumber", timeout=30)
        if rows:
            profile["manufacturer"] = rows[0].get("Manufacturer")
            profile["model"] = rows[0].get("Model")
        if bios:
            profile["serial_number"] = bios[0].get("SerialNumber")
    return profile


def _pending_reboot():
    if common.IS_WINDOWS:
        result = common.run_powershell(
            "Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Component Based Servicing\\RebootPending'", timeout=20)
        return "true" in result["stdout"].lower()
    import os
    return os.path.exists("/var/run/reboot-required")


def _network_check(target_host="", **_):
    gateway = native.network_check_windows() if common.IS_WINDOWS else native.network_check_gateway()
    result = {
        "adapters": common.adapters(),
        "gateway": gateway,
        "gateway_ping": common.ping(gateway) if gateway else None,
        "internet_ping": common.ping("1.1.1.1"),
        "dns": common.resolve("www.microsoft.com"),
    }
    if target_host:
        result["target_ping"] = common.ping(target_host)
        result["target_dns"] = common.resolve(target_host)
    return result


def _event_log_summary(**_):
    return collect_event_log_report()


def _security_status(**_):
    return collect_security_report()


def _hang_and_crash_events(hours=168, **_):
    if common.IS_WINDOWS:
        script = (
            "Get-WinEvent -FilterHashtable @{LogName='Application','System'; "
            f"Id=1000,1001,1002,41,6008,18,19,1; StartTime=(Get-Date).AddHours(-{hours})}} -MaxEvents 60 -ErrorAction SilentlyContinue "
            "| Select-Object @{N='Time';E={$_.TimeCreated.ToString('o')}},Id,ProviderName,LevelDisplayName,"
            "@{N='Message';E={$_.Message.Substring(0,[Math]::Min(220,$_.Message.Length))}}"
        )
        rows, _ = common.ps_json(script, timeout=90, depth=3)
        dumps, _ = common.ps_json(
            "Get-ChildItem C:\\Windows\\Minidump -ErrorAction SilentlyContinue | Select-Object Name,@{N='Time';E={$_.LastWriteTime.ToString('o')}},Length", timeout=30)
        labels = {1000: "App crash", 1002: "App hang", 41: "Unexpected shutdown/BSOD", 6008: "Unexpected shutdown", 1001: "BugCheck/WER", 18: "WHEA error", 19: "WHEA error", 1: "WHEA error"}
        for row in rows:
            row["category"] = labels.get(row.get("Id"), "Event")
        return {"available": True, "events": rows, "crash_dumps": dumps, "bsod_count": len(dumps)}
    result = common.run(["journalctl", "-p", "err", f"--since={hours} hours ago", "-n", "80", "--no-pager"], timeout=60)
    if result["rc"] != 0:
        result = common.run(["dmesg", "--level=err,crit"], timeout=30)
    lines = [line for line in result["stdout"].splitlines() if line.strip()]
    oom = [line for line in lines if "out of memory" in line.lower() or "oom-killer" in line.lower()]
    hung = [line for line in lines if "hung task" in line.lower()]
    return {"available": True, "events": [{"Message": line} for line in lines[-60:]], "oom_events": oom, "hung_tasks": hung}


def _not_responding(**_):
    if common.IS_WINDOWS:
        rows, _ = common.ps_json("Get-Process | Where-Object {$_.MainWindowHandle -ne 0 -and -not $_.Responding} | Select-Object Id,ProcessName,MainWindowTitle", timeout=40)
        return {"not_responding": rows, "count": len(rows)}
    import psutil
    stuck = []
    for process in psutil.process_iter(["pid", "name", "status"]):
        try:
            if process.info["status"] in (psutil.STATUS_ZOMBIE, getattr(psutil, "STATUS_DISK_SLEEP", "disk-sleep")):
                stuck.append({"pid": process.info["pid"], "name": process.info["name"], "status": process.info["status"]})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"not_responding": stuck, "count": len(stuck)}


def _storage_snapshot(max_seconds=60, **_):
    import os
    deadline = time.monotonic() + max_seconds
    base = common.home()
    folders = {
        "downloads": base / "Downloads",
        "desktop": base / "Desktop",
        "documents": base / "Documents",
        "videos": base / "Videos",
    }
    if common.IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        folders.update({"temp": base / "AppData" / "Local" / "Temp", "appdata_local": base / "AppData" / "Local",
                        "update_cache": common.Path(os.environ.get("WINDIR", "C:\\Windows")) / "SoftwareDistribution" / "Download"})
    else:
        folders.update({"temp": common.Path("/tmp"), "cache": base / ".cache"})
    measured = {}
    for name, path in folders.items():
        if common.Path(path).exists():
            size, files, done = common.folder_size(path, deadline)
            measured[name] = {"path": str(path), "bytes": size, "size": common.human(size), "files": files, "complete": done}
    return {"captured_at": time.time(), "folders": measured}


def _network_speed(size_mb=50, server_url=None, agent_token=None, **_):
    """Download then upload a buffer against the server's /api/agents/speedtest endpoint."""
    import requests
    if not server_url:
        return {"available": False, "note": "No server URL in agent context."}
    base = server_url.rstrip("/")
    headers = {"Authorization": f"Bearer {agent_token}"} if agent_token else {}
    out = {"test": "network", "size_mb": size_mb}
    try:
        start = time.perf_counter()
        download = requests.get(f"{base}/api/agents/speedtest/download", params={"size_mb": size_mb}, headers=headers, timeout=120, stream=True)
        downloaded = sum(len(chunk) for chunk in download.iter_content(1024 * 256))
        down_seconds = time.perf_counter() - start
        out["download_mbps"] = round(downloaded * 8 / 1_000_000 / down_seconds, 1)
        payload = b"0" * (size_mb * 1024 * 1024)
        start = time.perf_counter()
        requests.post(f"{base}/api/agents/speedtest/upload", data=payload, headers=headers, timeout=120)
        up_seconds = time.perf_counter() - start
        out["upload_mbps"] = round(len(payload) * 8 / 1_000_000 / up_seconds, 1)
        out["available"] = True
    except requests.RequestException as error:
        out["available"] = False
        out["error"] = str(error)
    return out


# Map catalog tool names to implementations.
HANDLERS = {
    "system_profile": _system_profile,
    "top_processes": common.top_processes,
    "browser_memory": common.browser_memory,
    "disk_usage": common.disk_usage,
    "largest_folders": common.largest_folders,
    "largest_files": common.largest_files,
    "storage_snapshot": _storage_snapshot,
    "cpu_spike_watch": common.cpu_spike_watch,
    "not_responding": _not_responding,
    "hang_and_crash_events": _hang_and_crash_events,
    "network_check": _network_check,
    "event_log_summary": _event_log_summary,
    "security_status": _security_status,
    "temperatures": stress.temperatures,
    "kill_process": common.kill_process,
    "cleanup_candidates": native.cleanup_candidates,
    "clean_junk": native.clean_junk,
    "installed_apps": native.installed_apps,
    "uninstall_leftovers": native.uninstall_leftovers,
    "uninstall_app": native.uninstall_app,
    "remove_app_entry": native.remove_app_entry,
    "startup_items": native.startup_items,
    "disable_startup_item": native.disable_startup_item,
    "services": native.services,
    "service_control": native.service_control,
    "driver_problems": native.driver_problems,
    "disk_health": native.disk_health,
    "battery_health": native.battery_health,
    "pending_updates": native.pending_updates,
    "install_updates": native.install_updates,
    "os_integrity_check": native.os_integrity_check,
    "repair_os": native.repair_os,
    "network_repair": native.network_repair,
    "create_restore_point": native.create_restore_point,
    "stress_cpu": stress.stress_cpu,
    "stress_ram": stress.stress_ram,
    "stress_disk": stress.stress_disk,
    "network_speed": _network_speed,
    "run_shell": None,  # wired in run_tool when shell is allowed
}

_CATALOG = load_catalog()
_TOOLS = tools_by_name(_CATALOG)


def available_tools(allow_shell=False):
    platform_name = current_platform()
    out = []
    for tool in _CATALOG["tools"]:
        if platform_name not in tool["platforms"]:
            continue
        if tool["kind"] == "shell" and not allow_shell:
            continue
        out.append(tool)
    return out


def run_tool(tool_name, args=None, ctx=None, allow_shell=False, server_url=None, agent_token=None):
    tool = _TOOLS.get(tool_name)
    if not tool:
        raise ToolValidationError(f"Unknown tool: {tool_name}")
    if current_platform() not in tool["platforms"]:
        raise ToolValidationError(f"{tool_name} is not available on {current_platform()}")
    if tool["kind"] == "shell" and not allow_shell:
        raise ToolValidationError("Remote shell is disabled on this agent (start it with --allow-shell to enable).")
    clean = validate_args(tool, args or {})
    handler = HANDLERS.get(tool_name)
    if tool_name == "run_shell":
        return _run_shell(ctx=ctx, **clean)
    if handler is None:
        raise ToolValidationError(f"{tool_name} has no implementation on this platform")
    kwargs = dict(clean)
    if ctx is not None:
        kwargs["ctx"] = ctx
    if tool_name == "network_speed":
        kwargs["server_url"], kwargs["agent_token"] = server_url, agent_token
    if tool_name in ("disable_startup_item", "clean_junk", "uninstall_app", "remove_app_entry", "create_restore_point") and ctx is not None:
        kwargs["backup_dir"] = ctx.backup_dir
    return handler(**kwargs)


def _run_shell(command="", shell="powershell", timeout_seconds=120, ctx=None, **_):
    if shell == "powershell" and common.IS_WINDOWS:
        result = common.run_powershell(command, timeout=timeout_seconds)
    elif shell == "cmd" and common.IS_WINDOWS:
        result = common.run(["cmd", "/c", command], timeout=timeout_seconds)
    else:
        result = common.run(command, timeout=timeout_seconds, shell=True)
    return {"command": command, "rc": result["rc"], "stdout": result["stdout"][-4000:], "stderr": result["stderr"][-2000:]}


def tool_needs_approval(tool_name):
    tool = _TOOLS.get(tool_name)
    return bool(tool and needs_approval(tool))

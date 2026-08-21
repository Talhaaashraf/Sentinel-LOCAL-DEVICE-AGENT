"""Standalone cross-platform health reporter for the central Device Health server."""

import datetime
import json
import logging
import os
import platform
import socket
import sys
import time
import uuid
from pathlib import Path

import psutil
import requests

try:
    from security_checks import collect_security_report
    from performance_checks import collect_performance_report
    from event_logs import collect_event_log_report
except ImportError:
    from .security_checks import collect_security_report
    from .performance_checks import collect_performance_report
    from .event_logs import collect_event_log_report

AGENT_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = AGENT_DIR / "agent_config.json"
LOG_PATH = AGENT_DIR / "agent_log.txt"
DEFAULT_INTERVAL = 10
SECURITY_REFRESH_SECONDS = 300
EVENT_LOG_REFRESH_SECONDS = 600
DEFAULT_PERFORMANCE_INTERVAL_SECONDS = 1800
logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_security_cache = {"data": None, "checked_at": 0}
_performance_cache = {"data": None, "checked_at": 0}
_event_log_cache = {"data": None, "checked_at": 0}


def get_security():
    """Refresh security posture on a fixed slow cadence; every check here is read-only."""
    now = time.time()
    if _security_cache["data"] is None or now - _security_cache["checked_at"] > SECURITY_REFRESH_SECONDS:
        try:
            _security_cache["data"] = collect_security_report()
        except Exception:
            logging.exception("Security check failed")
            _security_cache["data"] = _security_cache["data"] or {}
        _security_cache["checked_at"] = now
    return _security_cache["data"]


def get_event_logs():
    """Refresh recent OS event log entries on a fixed slow cadence; read-only."""
    now = time.time()
    if _event_log_cache["data"] is None or now - _event_log_cache["checked_at"] > EVENT_LOG_REFRESH_SECONDS:
        try:
            _event_log_cache["data"] = collect_event_log_report()
        except Exception:
            logging.exception("Event log check failed")
            _event_log_cache["data"] = _event_log_cache["data"] or {}
        _event_log_cache["checked_at"] = now
    return _event_log_cache["data"]


def get_performance(config):
    """Refresh the active benchmark suite on a configurable cadence.

    Unlike get_security(), this briefly writes to disk and burns CPU, so it is
    opt-out (enable_performance_benchmark: false in agent_config.json) and
    defaults to a much longer interval than the report cycle itself.
    """
    if not config.get("enable_performance_benchmark", True):
        return None
    interval = max(300, int(config.get("performance_interval_seconds", DEFAULT_PERFORMANCE_INTERVAL_SECONDS)))
    now = time.time()
    if _performance_cache["data"] is None or now - _performance_cache["checked_at"] > interval:
        try:
            _performance_cache["data"] = collect_performance_report()
        except Exception:
            logging.exception("Performance benchmark failed")
            _performance_cache["data"] = _performance_cache["data"] or {}
        _performance_cache["checked_at"] = now
    return _performance_cache["data"]


def human_size(value):
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def collect_report(config):
    frequency = psutil.cpu_freq()
    per_core = psutil.cpu_percent(interval=0.15, percpu=True)
    memory, swap = psutil.virtual_memory(), psutil.swap_memory()
    disks = []
    for partition in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (PermissionError, OSError):
            continue
        disks.append({"device": partition.device, "mountpoint": partition.mountpoint, "filesystem": partition.fstype, "total": human_size(usage.total), "used": human_size(usage.used), "free": human_size(usage.free), "usage_percent": round(usage.percent, 2)})
    stats = psutil.net_if_stats()
    interfaces = []
    for name, addresses in psutil.net_if_addrs().items():
        interfaces.append({"name": name, "status": "UP" if stats.get(name) and stats[name].isup else "DOWN", "ipv4_addresses": [item.address for item in addresses if item.family == 2]})
    battery = None
    try:
        sensed = psutil.sensors_battery()
        if sensed:
            battery = {"percent": round(sensed.percent, 2), "charging": bool(sensed.power_plugged), "time_remaining": str(datetime.timedelta(seconds=max(0, sensed.secsleft))) if sensed.secsleft >= 0 else "Unknown"}
    except (AttributeError, OSError):
        pass
    processes = []
    for process in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            info = process.info
            processes.append({"pid": info["pid"], "name": info.get("name") or "Unknown", "cpu_percent": round(info.get("cpu_percent") or 0, 2), "memory_percent": round(info.get("memory_percent") or 0, 2)})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"system": {"os_name": platform.system(), "os_version": platform.version(), "os_release": platform.release(), "architecture": platform.machine(), "processor": platform.processor() or "Unknown", "hostname": socket.gethostname()}, "cpu": {"physical_cores": psutil.cpu_count(logical=False), "logical_cores": psutil.cpu_count(logical=True), "max_frequency_mhz": frequency.max if frequency else None, "current_frequency_mhz": frequency.current if frequency else None, "per_core_usage_percent": per_core, "total_usage_percent": psutil.cpu_percent(interval=None)}, "memory": {"ram": {"total": human_size(memory.total), "available": human_size(memory.available), "used": human_size(memory.used), "usage_percent": memory.percent}, "swap": {"total": human_size(swap.total), "used": human_size(swap.used), "usage_percent": swap.percent}}, "disk": disks, "network": {"bytes_sent": human_size(psutil.net_io_counters().bytes_sent), "bytes_received": human_size(psutil.net_io_counters().bytes_recv), "interfaces": interfaces}, "battery": battery, "processes_count": len(processes), "top_processes": {"top_by_cpu": sorted(processes, key=lambda item: item["cpu_percent"], reverse=True)[:5], "top_by_memory": sorted(processes, key=lambda item: item["memory_percent"], reverse=True)[:5]}, "security": get_security(), "performance": get_performance(config), "event_logs": get_event_logs()}


def load_config():
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Create {CONFIG_PATH} with server_url, AGENT_TOKEN, and nickname")
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        config = json.load(config_file)
    if not config.get("AGENT_TOKEN") and config.get("token"):
        config["AGENT_TOKEN"] = config["token"]
    for key in ("server_url", "AGENT_TOKEN"):
        if not config.get(key):
            raise RuntimeError(f"agent_config.json requires {key} (or token)")
    return config


def save_config(config):
    with CONFIG_PATH.open("w", encoding="utf-8") as config_file:
        json.dump(config, config_file, indent=2)


def request_with_retry(method, url, **kwargs):
    delay = 1
    while True:
        try:
            response = requests.request(method, url, timeout=15, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            logging.warning("Server request failed: %s; retrying in %ss", error, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)


def register(config):
    if config.get("device_id"):
        return config["device_id"]
    payload = {"token": config["AGENT_TOKEN"], "hostname": socket.gethostname(), "os_type": platform.system(), "nickname": config.get("nickname", socket.gethostname())}
    response = request_with_retry("POST", config["server_url"].rstrip("/") + "/api/agents/register", json=payload)
    device_id = response.json()["device_id"]
    config["device_id"] = device_id
    save_config(config)
    return device_id


def run():
    config = load_config()
    device_id = register(config)
    previous = None
    interval = max(2, int(config.get("interval_seconds", DEFAULT_INTERVAL)))
    logging.info("Agent %s started for device %s", config.get("nickname", socket.gethostname()), device_id)
    while True:
        report = collect_report(config)
        cpu_before = (previous or {}).get("cpu", {}).get("total_usage_percent", 0)
        ram_before = (previous or {}).get("memory", {}).get("ram", {}).get("usage_percent", 0)
        cpu_now = report["cpu"]["total_usage_percent"]
        ram_now = report["memory"]["ram"]["usage_percent"]
        spike = cpu_now - cpu_before > 40 or ram_now - ram_before > 40
        payload = {"device_id": device_id, "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(), "diagnostics": report, "spike_detected": spike}
        try:
            request_with_retry("POST", config["server_url"].rstrip("/") + "/api/agents/report", headers={"Authorization": "Bearer " + config["AGENT_TOKEN"]}, json=payload)
            logging.info("Report sent; spike_detected=%s", spike)
        except Exception:
            logging.exception("Unexpected report failure")
        previous = report
        time.sleep(interval)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        logging.exception("Agent stopped during setup")
        raise

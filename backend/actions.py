"""Explicit registry of read-only diagnostic actions."""

import os
import tempfile

import psutil

from .collectors import get_top_processes


def list_top_processes():
    return {**get_top_processes(), "read_only": True}


def _directory_size(path):
    total = 0
    try:
        for root, _, files in os.walk(path):
            for filename in files:
                try:
                    total += os.path.getsize(os.path.join(root, filename))
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError):
        return None
    return total


def check_disk_cleanup_candidates():
    temp_path = tempfile.gettempdir()
    temp_size = _directory_size(temp_path)
    recycle_path = os.path.join(os.environ.get("SystemDrive", "C:"), "$Recycle.Bin")
    recycle_size = _directory_size(recycle_path) if os.path.exists(recycle_path) else None
    mb = lambda value: f"{value / (1024 ** 2):.2f} MB" if value is not None else "Not available"
    return {"temporary_folder": temp_path, "temporary_folder_size": mb(temp_size), "recycle_bin_size": mb(recycle_size), "message": "Inspected only; no files were deleted.", "read_only": True}


def check_startup_programs():
    if os.name != "nt":
        return {"available": False, "message": "Windows startup registry is not available on this platform.", "items": []}
    try:
        import winreg
        locations = [(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"), (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run")]
        items = []
        for hive, path in locations:
            try:
                with winreg.OpenKey(hive, path) as key:
                    for index in range(winreg.QueryInfoKey(key)[1]):
                        name, value, _ = winreg.EnumValue(key, index)
                        items.append({"name": name, "command": value})
            except (FileNotFoundError, PermissionError, OSError):
                continue
        return {"available": True, "items": items, "read_only": True}
    except ImportError:
        return {"available": False, "message": "Registry support is unavailable.", "items": []}


def check_network_details():
    try:
        connection_count = len(psutil.net_connections(kind="inet"))
    except (psutil.AccessDenied, OSError):
        connection_count = "Not available"
    stats = psutil.net_if_stats()
    return {"active_connections": connection_count, "interfaces": [{"name": name, "is_up": bool(stat.isup), "speed_mbps": stat.speed, "mtu": stat.mtu} for name, stat in stats.items()], "read_only": True}


SAFE_ACTIONS = {"list_top_processes": list_top_processes, "check_disk_cleanup_candidates": check_disk_cleanup_candidates, "check_startup_programs": check_startup_programs, "check_network_details": check_network_details}


def run_action(action_id):
    action = SAFE_ACTIONS.get(action_id)
    if action is None:
        raise ValueError("Unknown or disallowed action")
    return {"action_id": action_id, "result": action()}

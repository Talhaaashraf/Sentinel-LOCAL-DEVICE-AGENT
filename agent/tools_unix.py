"""Linux and macOS diagnostic and maintenance tools.

Called by remote_tools.py only on the matching platform. Read tools wrap the
standard system utilities (systemctl, journalctl, dpkg/rpm, smartctl, upower,
system_profiler); maintenance tools wrap the documented package-manager and
cleanup commands an administrator would run by hand.
"""

import os
import plistlib
import re
import time
from pathlib import Path

from tools_common import (IS_MAC, delete_contents, folder_size, home, human,
                          browser_cache_dirs, run, which)


# ============================================================================
# Installed apps
# ============================================================================

def installed_apps(search="", include_store_apps=False, limit=400, **_):
    apps = []
    if IS_MAC:
        apps = _mac_apps()
    else:
        apps = _linux_packages()
    if search:
        needle = search.lower()
        apps = [app for app in apps if needle in (app["name"] or "").lower() or needle in (app.get("publisher") or "").lower()]
    apps.sort(key=lambda app: (app["name"] or "").lower())
    return {"count": len(apps), "hidden_count": 0, "orphaned_count": sum(1 for a in apps if a.get("orphaned")),
            "apps": apps[:limit]}


def _mac_apps():
    apps = []
    for folder in ("/Applications", str(home() / "Applications")):
        try:
            for entry in os.scandir(folder):
                if entry.name.endswith(".app"):
                    version = None
                    try:
                        with open(Path(entry.path) / "Contents" / "Info.plist", "rb") as handle:
                            info = plistlib.load(handle)
                        version = info.get("CFBundleShortVersionString")
                    except (OSError, plistlib.InvalidFileException):
                        pass
                    apps.append({"id": f"app:{entry.path}", "name": entry.name.removesuffix(".app"), "version": version,
                                 "publisher": None, "install_location": entry.path, "has_display_name": True,
                                 "hidden_from_control_panel": False, "orphaned": False, "orphan_reasons": [], "source": "app_bundle"})
        except OSError:
            continue
    if which("brew"):
        result = run(["brew", "list", "--versions"], timeout=60)
        for line in result["stdout"].splitlines():
            parts = line.split()
            if parts:
                apps.append({"id": f"brew:{parts[0]}", "name": parts[0], "version": " ".join(parts[1:]) or None,
                             "publisher": "Homebrew", "install_location": None, "has_display_name": True,
                             "hidden_from_control_panel": False, "orphaned": False, "orphan_reasons": [], "source": "brew"})
    return apps


def _linux_packages():
    apps = []
    if which("dpkg-query"):
        result = run(["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Maintainer}\t${Status}\n"], timeout=60)
        for line in result["stdout"].splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                orphaned = "not-installed" in parts[3] or "config-files" in parts[3]
                apps.append({"id": f"dpkg:{parts[0]}", "name": parts[0], "version": parts[1], "publisher": parts[2],
                             "install_location": None, "has_display_name": True, "hidden_from_control_panel": False,
                             "orphaned": orphaned, "orphan_reasons": ["package removed but config files remain"] if orphaned else [],
                             "source": "dpkg"})
    elif which("rpm"):
        result = run(["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}\t%{VENDOR}\n"], timeout=60)
        for line in result["stdout"].splitlines():
            parts = line.split("\t")
            if parts:
                apps.append({"id": f"rpm:{parts[0]}", "name": parts[0], "version": parts[1] if len(parts) > 1 else None,
                             "publisher": parts[2] if len(parts) > 2 else None, "install_location": None,
                             "has_display_name": True, "hidden_from_control_panel": False, "orphaned": False,
                             "orphan_reasons": [], "source": "rpm"})
    for manager, command in (("snap", ["snap", "list"]), ("flatpak", ["flatpak", "list", "--columns=application,version"])):
        if which(manager):
            result = run(command, timeout=40)
            for line in result["stdout"].splitlines()[1:]:
                parts = line.split()
                if parts:
                    apps.append({"id": f"{manager}:{parts[0]}", "name": parts[0], "version": parts[1] if len(parts) > 1 else None,
                                 "publisher": manager, "install_location": None, "has_display_name": True,
                                 "hidden_from_control_panel": False, "orphaned": False, "orphan_reasons": [], "source": manager})
    return apps


def uninstall_leftovers(name="", **_):
    if not name:
        raise ValueError("Give the app or vendor name")
    needle = name.lower()
    folders = []
    roots = [home() / ".config", home() / ".cache", home() / ".local" / "share", Path("/opt"), Path("/usr/local")]
    if IS_MAC:
        roots += [home() / "Library" / "Application Support", home() / "Library" / "Preferences", home() / "Library" / "Caches"]
    for root in roots:
        try:
            for entry in os.scandir(root):
                if entry.is_dir() and needle in entry.name.lower():
                    size, files, _ = folder_size(entry.path, time.monotonic() + 4)
                    folders.append({"path": entry.path, "size": human(size), "files": files})
        except OSError:
            continue
    matches = installed_apps(search=name, limit=100)["apps"]
    return {"name": name, "folders": folders, "services": [], "scheduled_tasks": [],
            "registry_entries": [{"id": app["id"], "name": app["name"], "orphaned": app.get("orphaned")} for app in matches]}


def uninstall_app(app_id="", mode="standard", backup_dir=None, **_):
    try:
        kind, ref = app_id.split(":", 1)
    except ValueError as error:
        raise ValueError("Invalid app id") from error
    if kind == "dpkg":
        command = ["apt-get", "remove", "-y", ref] if mode == "standard" else ["apt-get", "purge", "-y", ref]
    elif kind == "rpm":
        command = ["dnf", "remove", "-y", ref] if which("dnf") else ["rpm", "-e", ref]
    elif kind == "snap":
        command = ["snap", "remove", ref]
    elif kind == "flatpak":
        command = ["flatpak", "uninstall", "-y", ref]
    elif kind == "brew":
        command = ["brew", "uninstall", ref]
    elif kind == "app":
        dest = Path(backup_dir) / "quarantine" / Path(ref).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        Path(ref).rename(dest)
        return {"app": Path(ref).name, "removed": True, "steps": [{"method": "move .app to quarantine", "to": str(dest)}]}
    else:
        raise ValueError(f"Unsupported app source: {kind}")
    if kind in ("dpkg", "rpm") and os.geteuid() != 0:
        command = ["sudo", "-n"] + command
    result = run(command, timeout=600)
    return {"app": ref, "removed": result["rc"] == 0, "steps": [{"method": " ".join(command), "rc": result["rc"], "output": (result["stdout"] + result["stderr"])[-400:]}]}


def remove_app_entry(app_id="", backup_dir=None, **_):
    return {"removed": False, "note": "Orphaned-entry removal is Windows-only; on this OS use uninstall_app with purge."}


# ============================================================================
# Startup, services, drivers, health
# ============================================================================

def startup_items(**_):
    items = []
    if IS_MAC:
        for label, folder in [("LaunchAgents-user", home() / "Library" / "LaunchAgents"),
                              ("LaunchAgents", Path("/Library/LaunchAgents")),
                              ("LaunchDaemons", Path("/Library/LaunchDaemons"))]:
            try:
                for entry in os.scandir(folder):
                    if entry.name.endswith(".plist"):
                        items.append({"id": f"plist:{entry.path}", "name": entry.name, "command": entry.path, "location": label})
            except OSError:
                continue
    else:
        for folder in (home() / ".config" / "autostart", Path("/etc/xdg/autostart")):
            try:
                for entry in os.scandir(folder):
                    if entry.name.endswith(".desktop"):
                        items.append({"id": f"autostart:{entry.path}", "name": entry.name, "command": entry.path, "location": str(folder)})
            except OSError:
                continue
        if which("systemctl"):
            result = run(["systemctl", "list-unit-files", "--type=service", "--state=enabled", "--no-legend", "--no-pager"], timeout=40)
            for line in result["stdout"].splitlines():
                parts = line.split()
                if parts:
                    items.append({"id": f"systemd:{parts[0]}", "name": parts[0], "command": "systemd service", "location": "systemd"})
    return {"items": items}


def disable_startup_item(item_id="", backup_dir=None, **_):
    kind, ref = item_id.split(":", 1)
    if kind == "systemd":
        command = ["systemctl", "disable", ref]
        if os.geteuid() != 0:
            command = ["sudo", "-n"] + command
        result = run(command, timeout=40)
        return {"disabled": item_id, "ok": result["rc"] == 0, "error": result["stderr"] or None}
    src = Path(ref)
    dest = Path(backup_dir) / "startup" / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)
    return {"disabled": item_id, "backup": str(dest)}


def services(state="all", search="", **_):
    rows = []
    if IS_MAC:
        result = run(["launchctl", "list"], timeout=40)
        for line in result["stdout"].splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 3:
                rows.append({"Name": parts[2], "State": "Running" if parts[0] not in ("-", "0") else "Stopped", "PID": parts[0]})
    elif which("systemctl"):
        result = run(["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--no-pager", "--plain"], timeout=40)
        for line in result["stdout"].splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 4:
                rows.append({"Name": parts[0], "Load": parts[1], "Active": parts[2], "State": parts[3]})
    if search:
        rows = [row for row in rows if search.lower() in row["Name"].lower()]
    if state == "running":
        rows = [row for row in rows if "running" in str(row).lower()]
    elif state in ("stopped", "failed"):
        rows = [row for row in rows if "failed" in str(row).lower() or "dead" in str(row).lower() or "stopped" in str(row).lower()]
    return {"count": len(rows), "services": rows[:400]}


def service_control(name="", action="restart", **_):
    if IS_MAC:
        mapping = {"start": "load", "stop": "unload", "restart": "kickstart"}
        result = run(["launchctl", mapping.get(action, "kickstart"), "-k", name] if action == "restart" else ["launchctl", mapping[action], name], timeout=60)
    else:
        command = ["systemctl", action, name]
        if os.geteuid() != 0:
            command = ["sudo", "-n"] + command
        result = run(command, timeout=90)
    return {"service": name, "action": action, "ok": result["rc"] == 0, "error": result["stderr"] or None}


def driver_problems(**_):
    if IS_MAC:
        return {"problem_devices": [], "count": 0, "note": "Driver error enumeration is not applicable on macOS."}
    result = run(["dmesg", "--level=err,warn", "--since", "1 hour ago"], timeout=30)
    if result["rc"] != 0:
        result = run(["dmesg"], timeout=30)
    lines = [line for line in result["stdout"].splitlines() if re.search(r"firmware|driver|error", line, re.I)][-40:]
    return {"problem_devices": [{"message": line} for line in lines], "count": len(lines)}


def disk_health(**_):
    disks = []
    if which("smartctl"):
        scan = run(["smartctl", "--scan"], timeout=20)
        for line in scan["stdout"].splitlines():
            device = line.split()[0] if line.split() else None
            if not device:
                continue
            info = run(["smartctl", "-H", "-A", device], timeout=30)
            healthy = "PASSED" in info["stdout"]
            disks.append({"device": device, "health": "PASSED" if healthy else "CHECK", "detail": info["stdout"][-400:]})
    elif IS_MAC:
        result = run(["diskutil", "info", "-all"], timeout=40)
        for block in result["stdout"].split("\n\n"):
            smart = re.search(r"SMART Status:\s*(\S+)", block)
            name = re.search(r"Device / Media Name:\s*(.+)", block)
            if smart:
                disks.append({"device": name.group(1).strip() if name else "disk", "health": smart.group(1)})
    return {"disks": disks, "note": None if disks else "Install smartmontools (smartctl) for SMART health."}


def battery_health(**_):
    if IS_MAC:
        result = run(["system_profiler", "SPPowerDataType"], timeout=40)
        cycles = re.search(r"Cycle Count:\s*(\d+)", result["stdout"])
        condition = re.search(r"Condition:\s*(.+)", result["stdout"])
        design = re.search(r"Maximum Capacity:\s*(\d+)", result["stdout"])
        return {"cycle_count": int(cycles.group(1)) if cycles else None, "condition": condition.group(1).strip() if condition else None,
                "max_capacity_percent": int(design.group(1)) if design else None, "available": bool(cycles)}
    base = Path("/sys/class/power_supply")
    for battery in base.glob("BAT*"):
        try:
            full = int((battery / "energy_full").read_text())
            design = int((battery / "energy_full_design").read_text())
            cycles = int((battery / "cycle_count").read_text()) if (battery / "cycle_count").exists() else None
            return {"design_capacity": design, "full_charge_capacity": full, "cycle_count": cycles,
                    "wear_percent": round((1 - full / design) * 100, 1) if design else None, "available": True}
        except (OSError, ValueError):
            continue
    return {"available": False, "note": "No battery data available."}


def pending_updates(ctx=None, **_):
    if IS_MAC:
        result = run(["softwareupdate", "-l"], timeout=180)
        pending = [line.strip("* ").strip() for line in result["stdout"].splitlines() if line.strip().startswith("*")]
        return {"available": True, "pending": [{"Title": item} for item in pending], "pending_count": len(pending), "reboot_required": False}
    if which("apt-get"):
        run(["apt-get", "update"], timeout=180)
        result = run(["apt-get", "-s", "upgrade"], timeout=120)
        pending = [line.split()[1] for line in result["stdout"].splitlines() if line.startswith("Inst")]
        reboot = Path("/var/run/reboot-required").exists()
        return {"available": True, "pending": [{"Title": item} for item in pending], "pending_count": len(pending), "reboot_required": reboot}
    if which("dnf"):
        result = run(["dnf", "check-update", "-q"], timeout=180)
        pending = [line.split()[0] for line in result["stdout"].splitlines() if line.strip() and not line.startswith(" ")]
        return {"available": True, "pending": [{"Title": item} for item in pending], "pending_count": len(pending), "reboot_required": False}
    return {"available": False, "pending": [], "pending_count": 0, "note": "No supported package manager found."}


def install_updates(include_drivers=False, ctx=None, **_):
    if ctx:
        ctx.progress({"stage": "installing updates"})
    if IS_MAC:
        result = run(["softwareupdate", "-i", "-a"], timeout=7000)
    elif which("apt-get"):
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        command = ["apt-get", "-y", "upgrade"]
        if os.geteuid() != 0:
            command = ["sudo", "-n"] + command
        result = run(command, timeout=7000, env=env)
    elif which("dnf"):
        command = ["dnf", "-y", "upgrade"]
        if os.geteuid() != 0:
            command = ["sudo", "-n"] + command
        result = run(command, timeout=7000)
    else:
        return {"ok": False, "note": "No supported package manager found."}
    return {"ok": result["rc"] == 0, "output_tail": (result["stdout"] + result["stderr"])[-1200:]}


def os_integrity_check(deep=False, ctx=None, **_):
    checks = []
    if IS_MAC:
        sip = run(["csrutil", "status"], timeout=20)
        checks.append({"name": "SIP status", "output": sip["stdout"]})
        verify = run(["diskutil", "verifyVolume", "/"], timeout=600)
        checks.append({"name": "Verify system volume", "rc": verify["rc"], "output": verify["stdout"][-600:]})
    else:
        if which("dpkg"):
            audit = run(["dpkg", "--audit"], timeout=60)
            checks.append({"name": "dpkg audit", "output": audit["stdout"][:600] or "No broken packages."})
        if which("systemctl"):
            failed = run(["systemctl", "--failed", "--no-legend", "--no-pager", "--plain"], timeout=40)
            checks.append({"name": "Failed services", "output": failed["stdout"] or "None failed."})
    return {"checks": checks}


def repair_os(step="", ctx=None, **_):
    if step == "package_repair":
        if IS_MAC:
            return {"step": step, "note": "Package repair is not applicable on macOS."}
        if which("dpkg"):
            command = ["dpkg", "--configure", "-a"]
        elif which("dnf"):
            command = ["dnf", "-y", "distro-sync"]
        else:
            return {"step": step, "note": "No supported package manager."}
        if os.geteuid() != 0:
            command = ["sudo", "-n"] + command
        result = run(command, timeout=1200)
        if which("apt-get"):
            run((["sudo", "-n"] if os.geteuid() != 0 else []) + ["apt-get", "-f", "install", "-y"], timeout=1200)
        return {"step": step, "rc": result["rc"], "output_tail": (result["stdout"] + result["stderr"])[-1200:], "success": result["rc"] == 0}
    if step == "verify_volume" and IS_MAC:
        result = run(["diskutil", "verifyVolume", "/"], timeout=900)
        return {"step": step, "rc": result["rc"], "output_tail": result["stdout"][-1200:], "success": result["rc"] == 0}
    return {"step": step, "note": f"Repair step {step!r} is not available on this OS."}


# ============================================================================
# Cleanup
# ============================================================================

def cleanup_candidates(**_):
    deadline = time.monotonic() + 40
    sizes = {}
    targets = {"temp": Path("/tmp"), "user_cache": home() / ".cache", "trash": home() / ".local" / "share" / "Trash"}
    if IS_MAC:
        targets = {"temp": Path("/private/var/folders"), "user_cache": home() / "Library" / "Caches", "trash": home() / ".Trash"}
    for name, path in targets.items():
        if path.exists():
            size, files, _ = folder_size(path, deadline)
            sizes[name] = {"path": str(path), "size": human(size), "bytes": size, "files": files}
    cache_total = sum(folder_size(cache, deadline)[0] for cache in browser_cache_dirs())
    sizes["browser_cache"] = {"size": human(cache_total), "bytes": cache_total}
    return {"candidates": sizes}


def clean_junk(targets=None, backup_dir=None, **_):
    targets = targets or ["temp"]
    freed_total, detail = 0, []
    deadline = time.monotonic() + 300
    for target in targets:
        folders = []
        if target == "temp":
            folders = [home() / ".cache" / "tmp"]
        elif target == "browser_cache":
            folders = browser_cache_dirs()
        elif target == "recycle_bin":
            folders = [home() / ".local" / "share" / "Trash" / "files"] if not IS_MAC else [home() / ".Trash"]
        elif target == "crash_dumps":
            folders = [Path("/var/crash")] if not IS_MAC else [home() / "Library" / "Logs" / "DiagnosticReports"]
        freed = deleted = 0
        for folder in folders:
            got, count, _ = delete_contents(folder, deadline=deadline)
            freed += got
            deleted += count
        freed_total += freed
        detail.append({"target": target, "freed": human(freed), "files_deleted": deleted})
    return {"freed_total": human(freed_total), "freed_bytes": freed_total, "detail": detail}


# ============================================================================
# Network and restore point stubs
# ============================================================================

def network_repair(actions=None, **_):
    actions = actions or ["flush_dns"]
    done = []
    for action in actions:
        if action == "flush_dns":
            if IS_MAC:
                run(["dscacheutil", "-flushcache"], timeout=20)
                result = run(["killall", "-HUP", "mDNSResponder"], timeout=20)
            elif which("resolvectl"):
                result = run((["sudo", "-n"] if os.geteuid() != 0 else []) + ["resolvectl", "flush-caches"], timeout=20)
            else:
                result = {"rc": 0, "stdout": "no-op"}
            done.append({"action": action, "rc": result["rc"]})
        elif action == "renew_ip":
            done.append({"action": action, "note": "Renew the lease via NetworkManager/dhclient manually on this OS."})
    return {"actions": done, "reboot_recommended": False}


def create_restore_point(description="", **_):
    return {"created": False, "note": "System restore points are Windows-only."}


def network_check_gateway():
    result = run(["ip", "route", "show", "default"], timeout=15) if not IS_MAC else run(["route", "-n", "get", "default"], timeout=15)
    match = re.search(r"(?:via|gateway:)\s*([\d.]+)", result["stdout"])
    return match.group(1) if match else None

"""Windows-specific diagnostic and maintenance tools.

Every function here is called by remote_tools.py only when the agent runs on
Windows. Read tools query WMI/registry/PowerShell; maintenance tools wrap the
standard Microsoft repair utilities (DISM, SFC, chkdsk) and the documented
uninstall/Windows-Update interfaces an administrator would use by hand.
"""

import os
import time
from pathlib import Path

from tools_common import (human, ps_json, run, run_powershell, delete_contents, folder_size,
                          record_backup, read_manifest, mark_restored)

try:
    import winreg
except ImportError:  # not Windows; functions below are never called there
    winreg = None

UNINSTALL_KEYS = [
    (r"HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (r"HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (r"HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]
_HIVES = {"HKLM": None, "HKCU": None}


def _hive(name):
    return winreg.HKEY_LOCAL_MACHINE if name == "HKLM" else winreg.HKEY_CURRENT_USER


# ============================================================================
# Installed apps, including hidden-from-Control-Panel and orphaned entries
# ============================================================================

def _read_app_entry(hive_name, base, subkey, flag):
    try:
        with winreg.OpenKey(_hive(hive_name), f"{base}\\{subkey}", 0, winreg.KEY_READ | flag) as key:
            values = {}
            for index in range(winreg.QueryInfoKey(key)[1]):
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                    values[name] = value
                except OSError:
                    continue
    except OSError:
        return None
    name = values.get("DisplayName")
    system_component = int(values.get("SystemComponent", 0) or 0)
    install_location = values.get("InstallLocation") or ""
    uninstall_string = values.get("UninstallString") or values.get("QuietUninstallString") or ""
    hidden = system_component == 1 or not name
    orphan_reasons = []
    if install_location and not os.path.isdir(install_location):
        orphan_reasons.append("install folder missing")
    exe = _uninstaller_path(uninstall_string)
    if exe and not os.path.exists(exe):
        orphan_reasons.append("uninstaller missing")
    return {
        "id": f"{hive_name}:{'32' if flag == winreg.KEY_WOW64_32KEY else '64'}:{subkey}",
        "name": name or subkey,
        "has_display_name": bool(name),
        "version": values.get("DisplayVersion"),
        "publisher": values.get("Publisher"),
        "install_location": install_location or None,
        "install_date": values.get("InstallDate"),
        "estimated_size": human(int(values.get("EstimatedSize", 0) or 0) * 1024) if values.get("EstimatedSize") else None,
        "uninstall_string": uninstall_string or None,
        "hidden_from_control_panel": bool(hidden),
        "orphaned": bool(orphan_reasons),
        "orphan_reasons": orphan_reasons,
        "source": "registry",
    }


def _uninstaller_path(uninstall_string):
    if not uninstall_string:
        return None
    text = uninstall_string.strip()
    if text.startswith('"'):
        return text[1:].split('"', 1)[0]
    lowered = text.lower()
    if lowered.startswith("msiexec"):
        return None
    return text.split(" /")[0].split(" -")[0].strip()


def installed_apps(search="", include_store_apps=False, limit=400, **_):
    seen, apps = set(), []
    for hive_name, base in UNINSTALL_KEYS:
        flags = [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY] if hive_name == "HKLM" else [0]
        for flag in flags:
            try:
                with winreg.OpenKey(_hive(hive_name), base, 0, winreg.KEY_READ | flag) as key:
                    count = winreg.QueryInfoKey(key)[0]
            except OSError:
                continue
            with winreg.OpenKey(_hive(hive_name), base, 0, winreg.KEY_READ | flag) as key:
                for index in range(count):
                    try:
                        subkey = winreg.EnumKey(key, index)
                    except OSError:
                        continue
                    entry = _read_app_entry(hive_name, base, subkey, flag)
                    if not entry:
                        continue
                    dedupe = (entry["name"], entry["version"])
                    if dedupe in seen:
                        continue
                    seen.add(dedupe)
                    apps.append(entry)
    if include_store_apps:
        rows, _ = ps_json("Get-AppxPackage | Select-Object Name,PackageFullName,Publisher,Version,InstallLocation", timeout=90)
        for row in rows:
            apps.append({
                "id": f"appx:{row.get('PackageFullName')}", "name": row.get("Name"), "has_display_name": True,
                "version": row.get("Version"), "publisher": row.get("Publisher"), "install_location": row.get("InstallLocation"),
                "uninstall_string": None, "hidden_from_control_panel": True, "orphaned": False, "orphan_reasons": [], "source": "store",
            })
    if search:
        needle = search.lower()
        apps = [app for app in apps if needle in (app["name"] or "").lower() or needle in (app["publisher"] or "").lower()]
    apps.sort(key=lambda app: (app["name"] or "").lower())
    return {
        "count": len(apps),
        "hidden_count": sum(1 for app in apps if app["hidden_from_control_panel"]),
        "orphaned_count": sum(1 for app in apps if app["orphaned"]),
        "apps": apps[:limit],
        "note": "hidden_from_control_panel or orphaned apps are the ones that will not show or uninstall normally.",
    }


def _find_app(app_id):
    for app in installed_apps(limit=5000, include_store_apps=app_id.startswith("appx:"))["apps"]:
        if app["id"] == app_id:
            return app
    return None


# ============================================================================
# Leftover finder (apps that will not uninstall / keep returning)
# ============================================================================

def uninstall_leftovers(name="", **_):
    if not name:
        raise ValueError("Give the app or vendor name")
    needle = name.lower()
    folders = []
    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramData"),
             os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")]
    for root in filter(None, roots):
        try:
            for entry in os.scandir(root):
                if entry.is_dir() and needle in entry.name.lower():
                    size, files, _ = folder_size(entry.path, time.monotonic() + 5)
                    folders.append({"path": entry.path, "size": human(size), "files": files})
        except OSError:
            continue
    services, _ = ps_json(f"Get-CimInstance Win32_Service | Where-Object {{$_.Name -like '*{name}*' -or $_.DisplayName -like '*{name}*'}} | Select-Object Name,DisplayName,State", timeout=40)
    tasks = run_powershell(f"Get-ScheduledTask | Where-Object {{$_.TaskName -like '*{name}*'}} | Select-Object -ExpandProperty TaskName", timeout=40)
    registry_hits = [app for app in installed_apps(search=name, limit=200)["apps"]]
    return {
        "name": name,
        "folders": folders,
        "services": services,
        "scheduled_tasks": [line for line in tasks["stdout"].splitlines() if line.strip()],
        "registry_entries": [{"id": app["id"], "name": app["name"], "orphaned": app["orphaned"]} for app in registry_hits],
    }


# ============================================================================
# Startup, services, drivers, updates, disk/battery health
# ============================================================================

def startup_items(**_):
    items = []
    for hive_name, path in [("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run"),
                            ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\Run"),
                            ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run")]:
        try:
            with winreg.OpenKey(_hive(hive_name), path) as key:
                for index in range(winreg.QueryInfoKey(key)[1]):
                    name, value, _ = winreg.EnumValue(key, index)
                    items.append({"id": f"{hive_name}:{path}:{name}", "name": name, "command": value, "location": f"{hive_name}\\{path}"})
        except OSError:
            continue
    for label, folder in [("startup-user", os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")),
                          ("startup-all", os.path.join(os.environ.get("ProgramData", ""), r"Microsoft\Windows\Start Menu\Programs\Startup"))]:
        try:
            for entry in os.scandir(folder):
                if entry.is_file():
                    items.append({"id": f"folder:{entry.path}", "name": entry.name, "command": entry.path, "location": label})
        except OSError:
            continue
    return {"items": items}


def disable_startup_item(item_id="", backup_dir=None, **_):
    if ":" not in item_id:
        raise ValueError("Invalid item id")
    kind, rest = item_id.split(":", 1)
    if kind == "folder":
        src = Path(rest)
        dest = Path(backup_dir) / "startup" / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dest)
        if backup_dir:
            record_backup(backup_dir, "startup_item", original=str(src), backup=str(dest), app=src.name,
                          extra={"startup_kind": "folder", "item_id": item_id})
        return {"disabled": item_id, "backup": str(dest)}
    hive_name, path, name = item_id.split(":", 2)
    with winreg.OpenKey(_hive(hive_name), path, 0, winreg.KEY_READ) as key:
        value, _ = winreg.QueryValueEx(key, name)
    with winreg.OpenKey(_hive(hive_name), path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.DeleteValue(key, name)
    if backup_dir:
        record_backup(backup_dir, "startup_item", original=f"{hive_name}\\{path}\\{name}", backup=None, app=name,
                      extra={"startup_kind": "registry", "hive": hive_name, "path": path, "name": name, "value": value, "item_id": item_id})
    return {"disabled": item_id, "restore_value": value}


def enable_startup_item(item_id="", backup_dir=None, **_):
    """Re-enable a startup item from its backup manifest entry."""
    for entry in read_manifest(backup_dir):
        if entry["type"] == "startup_item" and not entry.get("restored") and entry.get("extra", {}).get("item_id") == item_id:
            return restore_backup(entry_id=entry["id"], backup_dir=backup_dir)
    raise ValueError("No disabled-startup backup found for this item")


def restore_backup(entry_id="", backup_dir=None, **_):
    """Undo a reversible change recorded in the manifest."""
    entry = next((item for item in read_manifest(backup_dir) if item["id"] == entry_id), None)
    if not entry:
        raise ValueError("Backup entry not found")
    if entry.get("restored"):
        return {"entry_id": entry_id, "restored": False, "note": "Already restored."}
    kind = entry["type"]
    if kind == "quarantine_folder":
        src, dest = entry["backup"], entry["original"]
        if not src or not os.path.exists(src):
            raise ValueError("Quarantined folder is no longer available")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        try:
            Path(src).rename(dest)
        except OSError:
            run_powershell(f"Move-Item -LiteralPath {_ps_quote(src)} -Destination {_ps_quote(dest)} -Force", timeout=120)
    elif kind == "registry_export":
        if not entry["backup"] or not os.path.exists(entry["backup"]):
            raise ValueError("Registry backup (.reg) is no longer available")
        result = run(["reg", "import", entry["backup"]], timeout=30)
        if result["rc"] != 0:
            raise ValueError(f"reg import failed: {result['stderr']}")
    elif kind == "startup_item":
        extra = entry.get("extra", {})
        if extra.get("startup_kind") == "folder":
            src, dest = entry["backup"], entry["original"]
            if src and os.path.exists(src):
                Path(src).rename(dest)
        else:
            with winreg.OpenKey(_hive(extra["hive"]), extra["path"], 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, extra["name"], 0, winreg.REG_SZ, extra["value"])
    else:
        raise ValueError(f"Unknown backup type: {kind}")
    mark_restored(backup_dir, entry_id)
    return {"entry_id": entry_id, "restored": True, "type": kind, "target": entry.get("original")}


def services(state="all", search="", **_):
    rows, _ = ps_json("Get-CimInstance Win32_Service | Select-Object Name,DisplayName,State,StartMode", timeout=60)
    if search:
        needle = search.lower()
        rows = [row for row in rows if needle in (row.get("Name") or "").lower() or needle in (row.get("DisplayName") or "").lower()]
    if state == "running":
        rows = [row for row in rows if (row.get("State") or "").lower() == "running"]
    elif state in ("stopped", "failed"):
        rows = [row for row in rows if (row.get("State") or "").lower() == "stopped"]
    return {"count": len(rows), "services": rows[:400]}


def service_control(name="", action="restart", **_):
    if not name:
        raise ValueError("Give a service name")
    commands = {"start": f"Start-Service -Name '{name}'", "stop": f"Stop-Service -Name '{name}' -Force",
                "restart": f"Restart-Service -Name '{name}' -Force"}
    result = run_powershell(commands[action] + "; Get-Service -Name '" + name + "' | Select-Object -ExpandProperty Status", timeout=90)
    return {"service": name, "action": action, "ok": result["rc"] == 0, "status": result["stdout"], "error": result["stderr"] or None}


def driver_problems(**_):
    rows, _ = ps_json("Get-PnpDevice | Where-Object {$_.Status -ne 'OK' -and $_.Status -ne 'Unknown'} | Select-Object FriendlyName,Status,Class,Problem,InstanceId", timeout=60)
    return {"problem_devices": rows, "count": len(rows)}


def disk_health(**_):
    rows, _ = ps_json("Get-PhysicalDisk | Select-Object FriendlyName,MediaType,HealthStatus,OperationalStatus,@{N='SizeGB';E={[math]::Round($_.Size/1GB,1)}}", timeout=60)
    reliability, _ = ps_json("Get-PhysicalDisk | Get-StorageReliabilityCounter | Select-Object DeviceId,Temperature,Wear,ReadErrorsTotal,WriteErrorsTotal,PowerOnHours", timeout=60)
    return {"disks": rows, "reliability": reliability}


def battery_health(**_):
    report_dir = Path(os.environ.get("TEMP", r"C:\Windows\Temp"))
    report_path = report_dir / "sentinel_battery.xml"
    run(["powercfg", "/batteryreport", "/output", str(report_path), "/xml"], timeout=60)
    design = full = cycles = None
    try:
        text = report_path.read_text(encoding="utf-8", errors="ignore")
        import re
        design_match = re.search(r"<DesignCapacity>(\d+)", text)
        full_match = re.search(r"<FullChargeCapacity>(\d+)", text)
        cycle_match = re.search(r"<CycleCount>(\d+)", text)
        design = int(design_match.group(1)) if design_match else None
        full = int(full_match.group(1)) if full_match else None
        cycles = int(cycle_match.group(1)) if cycle_match else None
    except OSError:
        pass
    finally:
        try:
            report_path.unlink()
        except OSError:
            pass
    wear = round((1 - full / design) * 100, 1) if design and full else None
    return {"design_capacity_mwh": design, "full_charge_capacity_mwh": full, "cycle_count": cycles, "wear_percent": wear,
            "available": design is not None}


def pending_updates(**_):
    script = (
        "$s=New-Object -ComObject Microsoft.Update.Session;"
        "$r=($s.CreateUpdateSearcher()).Search('IsInstalled=0 and IsHidden=0');"
        "$r.Updates | ForEach-Object { [pscustomobject]@{Title=$_.Title; Severity=$_.MsrcSeverity; RebootRequired=$_.InstallationBehavior.RebootBehavior} }"
    )
    rows, result = ps_json(script, timeout=180)
    reboot = run_powershell("(New-Object -ComObject Microsoft.Update.SystemInfo).RebootRequired", timeout=30)
    last, _ = ps_json("Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 5 HotFixID,Description,@{N='InstalledOn';E={$_.InstalledOn.ToString('o')}}", timeout=60)
    return {
        "available": result["rc"] == 0,
        "pending": rows,
        "pending_count": len(rows),
        "reboot_required": "true" in reboot["stdout"].lower(),
        "recent_hotfixes": last,
        "error": result["stderr"] or None,
    }


# ============================================================================
# OS integrity
# ============================================================================

def os_integrity_check(deep=False, ctx=None, **_):
    result = {"checks": []}
    check = run(["dism", "/online", "/cleanup-image", "/checkhealth"], timeout=300)
    result["checks"].append({"name": "DISM CheckHealth", "rc": check["rc"], "output": check["stdout"][-600:]})
    if ctx:
        ctx.progress({"stage": "checkhealth done"})
    if deep:
        scan = run(["dism", "/online", "/cleanup-image", "/scanhealth"], timeout=1200)
        result["checks"].append({"name": "DISM ScanHealth", "rc": scan["rc"], "output": scan["stdout"][-600:]})
        verify = run(["sfc", "/verifyonly"], timeout=1200)
        text = verify["stdout"].replace("\x00", "")
        result["checks"].append({"name": "SFC verify", "rc": verify["rc"], "output": text[-600:]})
        result["violations_found"] = "did not find any integrity violations" not in text.lower()
    cbs = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Logs" / "CBS" / "CBS.log"
    if cbs.exists():
        try:
            tail = cbs.read_text(encoding="utf-8", errors="ignore")[-20000:]
            corrupt = tail.lower().count("cannot repair") + tail.lower().count("corrupt")
            result["cbs_log_corruption_mentions"] = corrupt
        except OSError:
            pass
    return result


# ============================================================================
# Cleanup
# ============================================================================

def cleanup_candidates(**_):
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    deadline = time.monotonic() + 40
    entries = {
        "temp_user": Path(os.environ.get("TEMP", "")),
        "temp_windows": windir / "Temp",
        "update_cache": windir / "SoftwareDistribution" / "Download",
        "windows_old": Path(os.environ.get("SystemDrive", "C:") + "\\Windows.old"),
        "crash_dumps": windir / "Minidump",
    }
    sizes = {}
    for name, path in entries.items():
        if path and path.exists():
            size, files, _ = folder_size(path, deadline)
            sizes[name] = {"path": str(path), "size": human(size), "bytes": size, "files": files}
    from tools_common import browser_cache_dirs
    cache_total = 0
    for cache in browser_cache_dirs():
        size, _, _ = folder_size(cache, deadline)
        cache_total += size
    sizes["browser_cache"] = {"size": human(cache_total), "bytes": cache_total}
    pagefile = run_powershell("(Get-CimInstance Win32_PageFileUsage | Measure-Object AllocatedBaseSize -Sum).Sum", timeout=20)
    return {"candidates": sizes, "pagefile_mb": pagefile["stdout"].strip() or None}


def clean_junk(targets=None, backup_dir=None, **_):
    targets = targets or ["temp"]
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    freed_total, detail = 0, []
    from tools_common import browser_cache_dirs
    deadline = time.monotonic() + 600
    for target in targets:
        folders = []
        if target == "temp":
            folders = [Path(os.environ.get("TEMP", "")), windir / "Temp"]
        elif target == "browser_cache":
            folders = browser_cache_dirs()
        elif target == "update_cache":
            run_powershell("Stop-Service wuauserv -Force -ErrorAction SilentlyContinue", timeout=40)
            folders = [windir / "SoftwareDistribution" / "Download"]
        elif target == "crash_dumps":
            folders = [windir / "Minidump"]
        elif target == "thumbnails":
            folders = [Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Explorer"]
        elif target == "recycle_bin":
            before = run_powershell("(Get-ChildItem -Path 'C:\\$Recycle.Bin' -Force -Recurse -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum", timeout=60)
            run_powershell("Clear-RecycleBin -Force -ErrorAction SilentlyContinue", timeout=120)
            freed = int(before["stdout"] or 0) if before["stdout"].isdigit() else 0
            freed_total += freed
            detail.append({"target": target, "freed": human(freed)})
            continue
        freed = deleted = 0
        for folder in folders:
            got, count, _ = delete_contents(folder, older_than_hours=1 if target == "temp" else 0, deadline=deadline)
            freed += got
            deleted += count
        freed_total += freed
        detail.append({"target": target, "freed": human(freed), "files_deleted": deleted})
    if "update_cache" in targets:
        run_powershell("Start-Service wuauserv -ErrorAction SilentlyContinue", timeout=40)
    return {"freed_total": human(freed_total), "freed_bytes": freed_total, "detail": detail}


# ============================================================================
# OS repair
# ============================================================================

def repair_os(step="", ctx=None, **_):
    commands = {
        "sfc": (["sfc", "/scannow"], 3000),
        "dism_restorehealth": (["dism", "/online", "/cleanup-image", "/restorehealth"], 3000),
        "dism_component_cleanup": (["dism", "/online", "/cleanup-image", "/startcomponentcleanup"], 1800),
        "chkdsk_scan": (["chkdsk", os.environ.get("SystemDrive", "C:"), "/scan"], 2400),
    }
    if step not in commands:
        raise ValueError(f"Unknown repair step for Windows: {step}")
    command, timeout = commands[step]
    if ctx:
        ctx.progress({"stage": f"running {step}"})
    result = run(command, timeout=timeout)
    text = result["stdout"].replace("\x00", "")
    return {"step": step, "rc": result["rc"], "output_tail": text[-1500:], "success": result["rc"] == 0}


# ============================================================================
# Uninstall (standard and force) and orphan-entry removal
# ============================================================================

def uninstall_app(app_id="", mode="standard", backup_dir=None, ctx=None, **_):
    app = _find_app(app_id)
    if not app:
        raise ValueError("App not found; run installed_apps first")
    steps = []
    if app["source"] == "store":
        full = app_id.split("appx:", 1)[1]
        result = run_powershell(f"Remove-AppxPackage -Package '{full}' -ErrorAction Stop", timeout=300)
        return {"app": app["name"], "removed": result["rc"] == 0, "steps": [{"method": "Remove-AppxPackage", "rc": result["rc"], "error": result["stderr"] or None}]}
    uninstall_string = app.get("uninstall_string") or ""
    if uninstall_string and mode in ("standard", "force"):
        command = _silent_uninstall_command(uninstall_string)
        result = run_powershell(f"Start-Process -Wait -PassThru cmd -ArgumentList '/c',{_ps_quote(command)} | Select-Object -ExpandProperty ExitCode", timeout=1000)
        steps.append({"method": "uninstall string", "command": command, "rc": result["rc"], "output": result["stdout"][-300:]})
        if _still_installed(app_id) is False:
            return {"app": app["name"], "removed": True, "steps": steps}
    if mode != "force":
        return {"app": app["name"], "removed": _still_installed(app_id) is False, "steps": steps,
                "note": "Standard uninstall finished. If it is still listed, run again with mode=force."}
    # Force path: stop its processes, quarantine its folder, remove the registry entry (backed up).
    location = app.get("install_location")
    if location and os.path.isdir(location):
        from tools_common import kill_process
        for exe in _exes_in(location):
            try:
                kill_process(name=exe)
            except Exception:
                pass
        quarantined = _quarantine(location, backup_dir)
        if backup_dir:
            record_backup(backup_dir, "quarantine_folder", original=location, backup=quarantined, app=app["name"])
        steps.append({"method": "quarantine install folder", "from": location, "to": quarantined})
    removed = remove_app_entry(app_id=app_id, backup_dir=backup_dir)
    steps.append({"method": "remove registry entry", **removed})
    return {"app": app["name"], "removed": True, "forced": True, "steps": steps,
            "note": "Folder moved to the quarantine folder (not deleted) so it can be restored if needed."}


def _silent_uninstall_command(uninstall_string):
    lowered = uninstall_string.lower()
    if "msiexec" in lowered:
        command = uninstall_string.replace("/I", "/X").replace("/i", "/x")
        if "/quiet" not in lowered and "/qn" not in lowered:
            command += " /quiet /norestart"
        return command
    for flag in ("/S", "/silent", "/verysilent", "--uninstall-silent"):
        if flag.lower() in lowered:
            return uninstall_string
    return uninstall_string + " /S"


def _ps_quote(text):
    return "'" + text.replace("'", "''") + "'"


def _exes_in(location):
    found = []
    try:
        for entry in os.scandir(location):
            if entry.is_file() and entry.name.lower().endswith(".exe"):
                found.append(entry.name)
    except OSError:
        pass
    return found[:30]


def _quarantine(location, backup_dir):
    target = Path(backup_dir) / "quarantine" / (Path(location).name + "_" + time.strftime("%Y%m%d%H%M%S"))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        Path(location).rename(target)
    except OSError:
        run_powershell(f"Move-Item -LiteralPath {_ps_quote(location)} -Destination {_ps_quote(str(target))} -Force", timeout=120)
    return str(target)


def _still_installed(app_id):
    try:
        return _find_app(app_id) is not None
    except Exception:
        return None


def remove_app_entry(app_id="", backup_dir=None, **_):
    try:
        hive_name, _, subkey = app_id.split(":", 2)
    except ValueError as error:
        raise ValueError("Invalid app id") from error
    base = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall" if ":32:" in app_id else r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    if hive_name == "HKCU":
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    full = f"{hive_name}\\{base}\\{subkey}"
    backup_path = None
    if backup_dir:
        backup_path = str(Path(backup_dir) / f"uninstall_{subkey}.reg")
        Path(backup_dir).mkdir(parents=True, exist_ok=True)
        run(["reg", "export", full, backup_path, "/y"], timeout=30)
    result = run(["reg", "delete", full, "/f"], timeout=30)
    if backup_dir and backup_path and result["rc"] == 0:
        record_backup(backup_dir, "registry_export", original=full, backup=backup_path, app=subkey)
    return {"entry": full, "removed": result["rc"] == 0, "backup": backup_path, "error": result["stderr"] or None}


# ============================================================================
# Updates, network, restore point
# ============================================================================

def install_updates(include_drivers=False, ctx=None, **_):
    script = (
        "$s=New-Object -ComObject Microsoft.Update.Session;"
        "$r=($s.CreateUpdateSearcher()).Search('IsInstalled=0 and IsHidden=0');"
        "$c=New-Object -ComObject Microsoft.Update.UpdateColl;"
        "foreach($u in $r.Updates){ $u.AcceptEula() | Out-Null; $c.Add($u) | Out-Null };"
        "if($c.Count -eq 0){ 'NOTHING_TO_INSTALL'; return };"
        "$d=$s.CreateUpdateDownloader(); $d.Updates=$c; $d.Download() | Out-Null;"
        "$i=$s.CreateUpdateInstaller(); $i.Updates=$c; $res=$i.Install();"
        "[pscustomobject]@{Installed=$c.Count; ResultCode=$res.ResultCode; RebootRequired=$res.RebootRequired} | ConvertTo-Json -Compress"
    )
    if ctx:
        ctx.progress({"stage": "downloading and installing updates"})
    result = run_powershell(script, timeout=7000)
    from tools_common import parse_json_output
    parsed = parse_json_output(result["stdout"])
    return {"ok": result["rc"] == 0, "result": parsed[0] if parsed else None,
            "message": "NOTHING_TO_INSTALL" if "NOTHING_TO_INSTALL" in result["stdout"] else None, "error": result["stderr"] or None}


def network_repair(actions=None, **_):
    actions = actions or ["flush_dns"]
    mapping = {
        "flush_dns": (["ipconfig", "/flushdns"], False),
        "renew_ip": (["ipconfig", "/renew"], False),
        "reset_winsock": (["netsh", "winsock", "reset"], True),
        "reset_tcpip": (["netsh", "int", "ip", "reset"], True),
    }
    done = []
    reboot = False
    for action in actions:
        if action not in mapping:
            continue
        command, needs_reboot = mapping[action]
        result = run(command, timeout=60)
        reboot = reboot or (needs_reboot and result["rc"] == 0)
        done.append({"action": action, "rc": result["rc"], "output": result["stdout"][-200:]})
    return {"actions": done, "reboot_recommended": reboot}


def create_restore_point(description="Sentinel before repair", **_):
    script = (
        "Enable-ComputerRestore -Drive $env:SystemDrive -ErrorAction SilentlyContinue;"
        f"Checkpoint-Computer -Description '{description}' -RestorePointType 'MODIFY_SETTINGS'"
    )
    result = run_powershell(script, timeout=300)
    return {"created": result["rc"] == 0, "description": description, "error": result["stderr"] or None,
            "note": "Windows limits restore points to one per 24h by default." if result["rc"] != 0 else None}


def network_check_windows():
    route = run_powershell("(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -First 1).NextHop", timeout=20)
    return route["stdout"].strip() or None

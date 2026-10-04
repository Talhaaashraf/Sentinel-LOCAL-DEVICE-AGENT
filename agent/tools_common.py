"""Cross-platform helpers and psutil-based tools used by remote_tools.py."""

import ctypes
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import psutil

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

BROWSERS = {
    "chrome": "Google Chrome",
    "msedge": "Microsoft Edge",
    "firefox": "Mozilla Firefox",
    "brave": "Brave",
    "opera": "Opera",
    "vivaldi": "Vivaldi",
    "safari": "Safari",
    "chromium": "Chromium",
    "google chrome": "Google Chrome",
    "microsoft edge": "Microsoft Edge",
}


# ============================================================================
# Process and shell helpers
# ============================================================================

def run(command, timeout=60, shell=False, env=None):
    """Run a command and always return a dict — never raises for a failed command."""
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, shell=shell, env=env,
            creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace",
        )
        return {"rc": completed.returncode, "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}
    except subprocess.TimeoutExpired as error:
        out = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        return {"rc": -1, "stdout": out.strip(), "stderr": f"Timed out after {timeout}s"}
    except (OSError, ValueError) as error:
        return {"rc": -2, "stdout": "", "stderr": str(error)}


def run_powershell(script, timeout=60):
    prefix = "$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    return run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", prefix + script], timeout=timeout)


def parse_json_output(text):
    """Parse ConvertTo-Json style output; a single object becomes a one-item list."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        return [data]
    return data if isinstance(data, list) else []


def ps_json(script, timeout=60, depth=4):
    result = run_powershell(f"{script} | ConvertTo-Json -Depth {depth} -Compress", timeout=timeout)
    return parse_json_output(result["stdout"]), result


def which(name):
    from shutil import which as _which
    return _which(name)


def is_admin():
    try:
        if IS_WINDOWS:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return None


def human(value):
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def home():
    return Path.home()


def user_temp_dirs():
    dirs = {Path(tempfile.gettempdir())}
    if IS_WINDOWS:
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        dirs.add(windir / "Temp")
    return [path for path in dirs if path.exists()]


# ============================================================================
# Folder size scanning with a deadline
# ============================================================================

def folder_size(path, deadline=None):
    """Return (bytes, file_count, complete). Skips links and unreadable folders."""
    total, count, stack = 0, 0, [str(path)]
    while stack:
        if deadline and time.monotonic() > deadline:
            return total, count, False
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if IS_WINDOWS and _is_reparse_point(entry):
                                continue
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count, True


def _is_reparse_point(entry):
    try:
        return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
    except (OSError, AttributeError):
        return False


def path_size(path, deadline=None):
    path = Path(path)
    try:
        if path.is_file():
            return path.stat().st_size, 1, True
    except OSError:
        return 0, 0, True
    if not path.exists():
        return 0, 0, True
    return folder_size(path, deadline)


# ============================================================================
# Read tools
# ============================================================================

def _process_rows(sample_seconds):
    procs = list(psutil.process_iter(["pid", "name", "username", "ppid"]))
    for process in procs:
        try:
            process.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(sample_seconds)
    cores = psutil.cpu_count() or 1
    total_ram = psutil.virtual_memory().total
    rows = []
    for process in procs:
        try:
            # Read memory/cpu fresh here rather than trusting process_iter's cached
            # snapshot, which can come back empty under a transient race.
            with process.oneshot():
                cpu = process.cpu_percent(None) / cores
                rss = process.memory_info().rss
                name = process.name()
                user = None
                try:
                    user = process.username()
                except (psutil.AccessDenied, KeyError):
                    pass
                try:
                    cmdline = " ".join(process.cmdline() or [])[:300]
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    cmdline = ""
            rows.append({
                "pid": process.pid,
                "ppid": process.info.get("ppid"),
                "name": name or "?",
                "cpu_percent": round(cpu, 1),
                "memory_bytes": rss,
                "memory": human(rss),
                "memory_percent": round(rss / total_ram * 100, 1) if total_ram else 0,
                "user": user,
                "cmdline": cmdline,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows


def top_processes(sort="memory", limit=15, sample_seconds=2, **_):
    rows = _process_rows(sample_seconds)
    key = "cpu_percent" if sort == "cpu" else "memory_bytes"
    rows.sort(key=lambda row: row[key], reverse=True)
    memory = psutil.virtual_memory()
    return {
        "sorted_by": sort,
        "total_cpu_percent": psutil.cpu_percent(interval=0.3),
        "ram_used_percent": memory.percent,
        "ram_total": human(memory.total),
        "process_count": len(rows),
        "processes": [{k: v for k, v in row.items() if k != "cmdline"} for row in rows[:limit]],
    }


def _browser_key(name):
    lowered = (name or "").lower().removesuffix(".exe")
    for key, label in BROWSERS.items():
        if lowered == key or lowered.startswith(key + " ") or lowered.startswith(key + "-"):
            return label
    if "safari" in lowered and IS_MAC:
        return "Safari"
    return None


def _child_type(cmdline):
    match = re.search(r"--type=([\w-]+)", cmdline or "")
    if match:
        return match.group(1)
    if "-contentproc" in (cmdline or ""):
        return "content"
    return "browser"


def browser_memory(**_):
    rows = _process_rows(1)
    total_ram = psutil.virtual_memory().total
    browsers = {}
    for row in rows:
        label = _browser_key(row["name"])
        if not label:
            continue
        group = browsers.setdefault(label, {"browser": label, "memory_bytes": 0, "cpu_percent": 0.0, "process_count": 0, "types": {}, "heaviest": []})
        group["memory_bytes"] += row["memory_bytes"]
        group["cpu_percent"] += row["cpu_percent"]
        group["process_count"] += 1
        kind = _child_type(row["cmdline"])
        if "extension" in row["cmdline"]:
            kind = "extension"
        group["types"][kind] = group["types"].get(kind, 0) + 1
        group["heaviest"].append({"pid": row["pid"], "type": kind, "memory": row["memory"], "memory_bytes": row["memory_bytes"], "cpu_percent": row["cpu_percent"]})
    result = []
    for group in browsers.values():
        group["heaviest"] = sorted(group["heaviest"], key=lambda item: item["memory_bytes"], reverse=True)[:8]
        group["memory"] = human(group["memory_bytes"])
        group["percent_of_ram"] = round(group["memory_bytes"] / total_ram * 100, 1) if total_ram else 0
        group["cpu_percent"] = round(group["cpu_percent"], 1)
        group["approx_tabs"] = group["types"].get("renderer", 0) + group["types"].get("content", 0) + group["types"].get("tab", 0)
        group["extension_processes"] = group["types"].get("extension", 0)
        result.append(group)
    result.sort(key=lambda item: item["memory_bytes"], reverse=True)
    return {
        "ram_total": human(total_ram),
        "ram_used_percent": psutil.virtual_memory().percent,
        "browsers": result,
        "note": "approx_tabs counts renderer/content processes; one process can host several tabs.",
    }


def disk_usage(**_):
    volumes = []
    for partition in psutil.disk_partitions(all=False):
        if "cdrom" in partition.opts or partition.fstype in ("", "squashfs", "overlay", "tmpfs"):
            continue
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (PermissionError, OSError):
            continue
        volumes.append({
            "device": partition.device, "mountpoint": partition.mountpoint, "filesystem": partition.fstype,
            "total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free,
            "total": human(usage.total), "used": human(usage.used), "free": human(usage.free),
            "percent": usage.percent,
        })
    return {"volumes": volumes}


def _resolve_scan_path(path):
    target = Path(os.path.expandvars(os.path.expanduser(path))) if path else home()
    if not target.exists():
        raise ValueError(f"Path not found: {target}")
    return target


def largest_folders(path="", limit=15, max_seconds=30, **_):
    target = _resolve_scan_path(path)
    deadline = time.monotonic() + max_seconds
    rows, complete = [], True
    loose_files = 0
    try:
        entries = list(os.scandir(target))
    except OSError as error:
        raise ValueError(f"Cannot read {target}: {error}") from error
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_file(follow_symlinks=False):
                loose_files += entry.stat(follow_symlinks=False).st_size
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        size, files, done = folder_size(entry.path, deadline)
        complete = complete and done
        rows.append({"path": entry.path, "bytes": size, "size": human(size), "files": files, "complete": done})
    rows.sort(key=lambda row: row["bytes"], reverse=True)
    return {"path": str(target), "files_directly_in_folder": human(loose_files), "complete": complete, "folders": rows[:limit],
            "note": None if complete else "Scan hit its time limit; sizes marked complete=false are lower bounds."}


def largest_files(path="", min_mb=200, limit=20, max_seconds=30, **_):
    target = _resolve_scan_path(path)
    deadline = time.monotonic() + max_seconds
    minimum = min_mb * 1024 * 1024
    found, stack, complete = [], [str(target)], True
    while stack:
        if time.monotonic() > deadline:
            complete = False
            break
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            if stat.st_size >= minimum:
                                found.append({"path": entry.path, "bytes": stat.st_size, "size": human(stat.st_size), "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))})
                    except OSError:
                        continue
        except OSError:
            continue
    found.sort(key=lambda row: row["bytes"], reverse=True)
    return {"path": str(target), "min_mb": min_mb, "complete": complete, "files": found[:limit]}


def cpu_spike_watch(seconds=20, ctx=None, **_):
    cores = psutil.cpu_count() or 1
    seen = {}
    for process in psutil.process_iter():
        try:
            process.cpu_percent(None)
            seen[process.pid] = process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    psutil.cpu_percent(None)
    samples, culprits = [], {}
    for second in range(seconds):
        time.sleep(1)
        total = psutil.cpu_percent(None)
        for process in psutil.process_iter():
            if process.pid not in seen:
                try:
                    process.cpu_percent(None)
                    seen[process.pid] = process
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                continue
        top = []
        for pid, process in list(seen.items()):
            try:
                usage = process.cpu_percent(None) / cores
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                seen.pop(pid, None)
                continue
            if usage >= 5:
                try:
                    name = process.name()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    name = "?"
                top.append((usage, pid, name))
                entry = culprits.setdefault(name, {"name": name, "max_cpu_percent": 0, "seconds_above_25": 0, "pids": set()})
                entry["max_cpu_percent"] = max(entry["max_cpu_percent"], round(usage, 1))
                entry["seconds_above_25"] += 1 if usage >= 25 else 0
                entry["pids"].add(pid)
        top.sort(reverse=True)
        sample = {"t": second + 1, "cpu": total, "top": [{"name": n, "pid": p, "cpu": round(u, 1)} for u, p, n in top[:3]]}
        samples.append(sample)
        if ctx and ctx.progress({"sample": sample}):
            break
    spikes = [sample for sample in samples if sample["cpu"] >= 80]
    ranked = sorted(culprits.values(), key=lambda item: (item["seconds_above_25"], item["max_cpu_percent"]), reverse=True)
    for item in ranked:
        item["pids"] = sorted(item["pids"])[:10]
    return {
        "seconds": len(samples),
        "average_cpu": round(sum(s["cpu"] for s in samples) / max(1, len(samples)), 1),
        "max_cpu": max((s["cpu"] for s in samples), default=0),
        "spike_seconds": len(spikes),
        "culprits": ranked[:10],
        "spike_samples": spikes[:20],
    }


def common_temperatures():
    result = {"sensors": [], "fans": []}
    try:
        for chip, entries in (psutil.sensors_temperatures() or {}).items():
            for entry in entries:
                result["sensors"].append({"chip": chip, "label": entry.label or chip, "current_c": entry.current, "high_c": entry.high, "critical_c": entry.critical})
    except (AttributeError, OSError):
        pass
    try:
        for chip, entries in (psutil.sensors_fans() or {}).items():
            for entry in entries:
                result["fans"].append({"chip": chip, "label": entry.label or chip, "rpm": entry.current})
    except (AttributeError, OSError):
        pass
    frequency = psutil.cpu_freq()
    if frequency:
        result["cpu_clock_mhz"] = round(frequency.current)
        result["cpu_max_clock_mhz"] = round(frequency.max) if frequency.max else None
        if frequency.max:
            result["clock_percent_of_max"] = round(frequency.current / frequency.max * 100)
    result["cpu_load_percent"] = psutil.cpu_percent(interval=0.5)
    return result


def max_cpu_temperature():
    try:
        temps = psutil.sensors_temperatures() or {}
    except (AttributeError, OSError):
        return None
    values = [entry.current for entries in temps.values() for entry in entries if entry.current and 10 < entry.current < 130]
    return max(values) if values else None


def ping(host, count=3, timeout=4):
    if IS_WINDOWS:
        command = ["ping", "-n", str(count), "-w", str(timeout * 1000), host]
    else:
        command = ["ping", "-c", str(count), "-W", str(timeout), host]
    result = run(command, timeout=count * timeout + 5)
    return parse_ping(result["stdout"], host)


def parse_ping(output, host=""):
    loss = re.search(r"(\d+(?:\.\d+)?)% (?:packet )?loss", output)
    average = re.search(r"(?:Average|Mittelwert|Moyenne) = (\d+)ms", output) or re.search(r"= [\d.]+/([\d.]+)/", output)
    received = None
    if loss:
        received = float(loss.group(1)) < 100
    return {
        "host": host,
        "reachable": bool(received),
        "loss_percent": float(loss.group(1)) if loss else None,
        "average_ms": float(average.group(1)) if average else None,
    }


def resolve(host):
    started = time.perf_counter()
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
        return {"host": host, "ok": True, "addresses": addresses[:4], "ms": round((time.perf_counter() - started) * 1000, 1)}
    except OSError as error:
        return {"host": host, "ok": False, "error": str(error)}


def adapters():
    stats = psutil.net_if_stats()
    rows = []
    for name, addresses in psutil.net_if_addrs().items():
        stat = stats.get(name)
        ipv4 = [item.address for item in addresses if item.family == socket.AF_INET]
        if not ipv4 and not (stat and stat.isup):
            continue
        rows.append({"name": name, "up": bool(stat and stat.isup), "speed_mbps": stat.speed if stat else None, "ipv4": ipv4})
    return rows


def kill_process(pid=0, name="", include_children=True, **_):
    if not pid and not name:
        raise ValueError("Give a pid or a process name")
    targets = []
    if pid:
        targets.append(psutil.Process(pid))
    else:
        wanted = name.lower()
        for process in psutil.process_iter(["name"]):
            process_name = (process.info.get("name") or "").lower()
            if process_name == wanted or process_name.removesuffix(".exe") == wanted.removesuffix(".exe"):
                targets.append(process)
    if not targets:
        return {"killed": [], "message": "No matching process was running."}
    protected = {"system", "csrss.exe", "wininit.exe", "winlogon.exe", "lsass.exe", "smss.exe", "services.exe", "launchd", "init", "systemd", "kernel_task"}
    own = os.getpid()
    victims = []
    for process in targets:
        try:
            if process.pid in (0, 1, own) or process.name().lower() in protected:
                continue
            victims.append(process)
            if include_children:
                victims.extend(process.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    killed, failed = [], []
    for process in victims:
        try:
            label = f"{process.name()} ({process.pid})"
            process.terminate()
            killed.append(label)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            failed.append(f"{process.pid}: access denied (run the agent as administrator)")
    gone, alive = psutil.wait_procs(victims, timeout=5)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return {"killed": killed, "failed": failed, "force_killed": len(alive)}


def system_basics():
    boot = psutil.boot_time()
    memory = psutil.virtual_memory()
    frequency = psutil.cpu_freq()
    return {
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "processor": platform.processor() or None,
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "max_clock_mhz": round(frequency.max) if frequency and frequency.max else None,
        "ram_total": human(memory.total),
        "ram_total_bytes": memory.total,
        "ram_used_percent": memory.percent,
        "uptime_hours": round((time.time() - boot) / 3600, 1),
        "boot_time": time.strftime("%Y-%m-%d %H:%M", time.localtime(boot)),
        "is_admin": is_admin(),
        "python": platform.python_version(),
        "volumes": disk_usage()["volumes"],
    }


def delete_contents(folder, older_than_hours=0, deadline=None):
    """Delete files inside folder (not the folder). Returns (freed_bytes, deleted, skipped)."""
    folder = Path(folder)
    freed, deleted, skipped = 0, 0, 0
    if not folder.exists():
        return 0, 0, 0
    cutoff = time.time() - older_than_hours * 3600
    for root, dirs, files in os.walk(folder, topdown=False):
        if deadline and time.monotonic() > deadline:
            break
        for filename in files:
            path = os.path.join(root, filename)
            try:
                stat = os.lstat(path)
                if older_than_hours and stat.st_mtime > cutoff:
                    skipped += 1
                    continue
                os.remove(path)
                freed += stat.st_size
                deleted += 1
            except OSError:
                skipped += 1
        for dirname in dirs:
            try:
                os.rmdir(os.path.join(root, dirname))
            except OSError:
                pass
    return freed, deleted, skipped


# ============================================================================
# Backup manifest — every reversible change is recorded so it can be undone
# ============================================================================

def manifest_path(backup_dir):
    return Path(backup_dir) / "manifest.json"


def read_manifest(backup_dir):
    path = manifest_path(backup_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def record_backup(backup_dir, entry_type, original, backup, app="", extra=None):
    """Append one reversible action to the manifest and return its entry.

    entry_type: 'quarantine_folder' | 'registry_export' | 'startup_item'
    original:   where it belongs (restore target); backup: where it is now kept.
    """
    Path(backup_dir).mkdir(parents=True, exist_ok=True)
    entries = read_manifest(backup_dir)
    entry = {
        "id": f"bk-{uuid.uuid4().hex[:10]}",
        "type": entry_type,
        "app": app,
        "original": str(original) if original else None,
        "backup": str(backup) if backup else None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "restored": False,
        "extra": extra or {},
    }
    entries.append(entry)
    manifest_path(backup_dir).write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return entry


def mark_restored(backup_dir, entry_id):
    entries = read_manifest(backup_dir)
    for entry in entries:
        if entry["id"] == entry_id:
            entry["restored"] = True
            entry["restored_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    manifest_path(backup_dir).write_text(json.dumps(entries, indent=2), encoding="utf-8")


def list_backups(backup_dir=None, **_):
    """Read tool: every recorded backup and whether its file still exists to restore from."""
    entries = read_manifest(backup_dir) if backup_dir else []
    for entry in entries:
        backup = entry.get("backup")
        entry["available"] = bool(backup and os.path.exists(backup)) if entry["type"] != "registry_export" else bool(backup and os.path.exists(backup))
    active = [entry for entry in entries if not entry.get("restored")]
    return {"backups": list(reversed(entries)), "restorable_count": sum(1 for entry in active if entry.get("available"))}


def browser_cache_dirs():
    base = home()
    if IS_WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA", base / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", base / "AppData" / "Roaming"))
        candidates = []
        for vendor in ("Google/Chrome", "Microsoft/Edge", "BraveSoftware/Brave-Browser", "Chromium", "Vivaldi"):
            user_data = local / vendor / "User Data"
            if user_data.exists():
                for profile in user_data.iterdir():
                    for sub in ("Cache", "Code Cache", "GPUCache", "Service Worker/CacheStorage"):
                        candidates.append(profile / sub)
        candidates += list((local / "Mozilla" / "Firefox" / "Profiles").glob("*/cache2"))
        candidates.append(roaming / "Opera Software" / "Opera Stable" / "Cache")
    elif IS_MAC:
        caches = base / "Library" / "Caches"
        candidates = [caches / "Google" / "Chrome", caches / "com.microsoft.edgemac", caches / "Firefox", caches / "com.apple.Safari", caches / "BraveSoftware"]
    else:
        cache = base / ".cache"
        candidates = [cache / "google-chrome", cache / "chromium", cache / "mozilla" / "firefox", cache / "microsoft-edge", cache / "BraveSoftware"]
    return [path for path in candidates if path.exists()]

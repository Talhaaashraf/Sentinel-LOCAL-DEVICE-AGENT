"""Rule-based diagnosis playbooks.

Each playbook names the read tools to run for a kind of problem and an analyzer
that turns their results into findings and proposed fixes. They serve three
purposes: one-click presets in the dashboard, starting hints for the LLM, and a
complete fallback diagnosis when no LLM is available.
"""

import re

from .typos import normalize

PLAYBOOKS = {
    "browser_ram": {
        "label": "Browser using a lot of RAM",
        "keywords": ["browser", "chrome", "edge", "firefox", "brave", "tab", "tabs", "opera"],
        "tools": [("browser_memory", {}), ("top_processes", {"sort": "memory"})],
    },
    "memory": {
        "label": "High memory / slow PC",
        "keywords": ["ram", "memory", "slow", "sluggish", "lag", "laggy"],
        "tools": [("top_processes", {"sort": "memory"}), ("browser_memory", {}), ("startup_items", {})],
    },
    "storage": {
        "label": "Unusual storage spike / disk full",
        "keywords": ["storage", "disk", "space", "full", "drive", "spike", "gb", "filling", "c:"],
        "tools": [("disk_usage", {}), ("storage_snapshot", {}), ("largest_folders", {}), ("cleanup_candidates", {})],
    },
    "cpu": {
        "label": "CPU spike / fan loud / hot",
        "keywords": ["cpu", "processor", "fan", "hot", "heat", "overheating", "100%", "spike", "usage"],
        "tools": [("cpu_spike_watch", {"seconds": 20}), ("top_processes", {"sort": "cpu"}), ("temperatures", {})],
    },
    "hang": {
        "label": "Hanging / freezing / crashing",
        "keywords": ["hang", "hanging", "freeze", "freezes", "freezing", "stuck", "responding", "crash", "crashing", "bsod", "blue", "restart", "restarts"],
        "tools": [("not_responding", {}), ("hang_and_crash_events", {}), ("disk_health", {}), ("driver_problems", {}), ("top_processes", {"sort": "cpu"})],
    },
    "os_corrupt": {
        "label": "OS corrupt / system files",
        "keywords": ["corrupt", "corrupted", "sfc", "dism", "system", "file", "boot", "repair"],
        "tools": [("os_integrity_check", {}), ("hang_and_crash_events", {}), ("disk_health", {})],
    },
    "uninstall": {
        "label": "App won't uninstall / not in Control Panel",
        "keywords": ["uninstall", "uninstalling", "remove", "removing", "control", "panel", "programs", "features", "app", "software"],
        "tools": [("installed_apps", {}), ("uninstall_leftovers", {})],
    },
    "updates": {
        "label": "System updates",
        "keywords": ["update", "updates", "updating", "patch", "patches", "upgrade"],
        "tools": [("pending_updates", {}), ("os_integrity_check", {}), ("disk_usage", {})],
    },
    "network": {
        "label": "Internet / Wi-Fi problems",
        "keywords": ["internet", "wifi", "network", "dns", "connection", "connect", "ethernet", "offline"],
        "tools": [("network_check", {})],
    },
    "battery": {
        "label": "Battery drain / health",
        "keywords": ["battery", "charge", "charging", "drain", "draining"],
        "tools": [("battery_health", {}), ("top_processes", {"sort": "cpu"})],
    },
    "startup": {
        "label": "Slow startup / boot",
        "keywords": ["startup", "boot", "booting", "login", "logon"],
        "tools": [("startup_items", {}), ("services", {"state": "running"}), ("disk_health", {})],
    },
    "health_check": {
        "label": "Full health check",
        "keywords": ["check", "health", "general", "everything", "overall", "full"],
        "tools": [("system_profile", {}), ("top_processes", {}), ("disk_usage", {}), ("hang_and_crash_events", {}), ("security_status", {}), ("battery_health", {})],
    },
}

STOPWORDS = {"the", "a", "is", "my", "and", "or", "of", "to", "in", "it", "not", "on", "using", "lot", "very", "too", "much", "issue", "issues", "laptop", "pc", "computer", "diagnose", "problem", "with", "that", "this", "for", "showing", "from", "won't", "wont", "cant", "can't", "doesnt", "does", "be"}


def presets():
    return [{"id": key, "label": value["label"], "tools": [tool for tool, _ in value["tools"]]} for key, value in PLAYBOOKS.items()]


def classify(problem):
    """Return (category_ids ranked, corrected_text, corrections)."""
    corrected, corrections = normalize(problem)
    words = set(re.findall(r"[a-z0-9%:]+", corrected.lower()))
    scores = {}
    for key, playbook in PLAYBOOKS.items():
        score = sum(1 for keyword in playbook["keywords"] if keyword in words or keyword in corrected.lower())
        if score:
            scores[key] = score
    ranked = sorted(scores, key=lambda key: scores[key], reverse=True)
    return ranked or ["health_check"], corrected, corrections


def guess_app_name(problem):
    """Pull a likely app name out of 'cannot uninstall McAfee' style text."""
    corrected, _ = normalize(problem)
    match = re.search(r"(?:uninstall|remove|removing|uninstalling|delete)\s+([A-Za-z0-9][\w .+-]{1,40})", corrected, re.I)
    if match:
        words = [word for word in match.group(1).split() if word.lower() not in STOPWORDS]
        if words:
            return " ".join(words[:3])
    quoted = re.search(r"['\"]([^'\"]{2,40})['\"]", problem or "")
    return quoted.group(1) if quoted else ""


def plan_tools(categories, problem=""):
    """Tools (with args) for the top two categories, de-duplicated."""
    plan, seen = [], set()
    app_name = guess_app_name(problem)
    for category in categories[:2]:
        for tool, args in PLAYBOOKS[category]["tools"]:
            args = dict(args)
            if tool in ("installed_apps",) and app_name:
                args["search"] = app_name
            if tool == "uninstall_leftovers":
                if not app_name:
                    continue
                args["name"] = app_name
            key = (tool, tuple(sorted(args.items())))
            if key in seen:
                continue
            seen.add(key)
            plan.append((tool, args))
    return plan


# ============================================================================
# Analyzers: tool results -> findings + proposed fixes
# ============================================================================

def _finding(severity, title, detail):
    return {"severity": severity, "title": title, "detail": detail}


def _fix(tool, args, reason):
    return {"tool": tool, "args": args, "reason": reason}


def analyze(results):
    """results: {tool_name: result_dict}. Returns {"findings": [...], "fixes": [...]}."""
    findings, fixes = [], []

    browser = results.get("browser_memory") or {}
    for group in browser.get("browsers", []):
        if group.get("percent_of_ram", 0) >= 25 or group.get("memory_bytes", 0) >= 3 * 1024 ** 3:
            findings.append(_finding("high", f"{group['browser']} uses {group['memory']} ({group['percent_of_ram']}% of RAM)",
                                     f"{group['process_count']} processes, about {group.get('approx_tabs', 0)} tab processes and {group.get('extension_processes', 0)} extension processes. "
                                     "Close unused tabs, remove heavy extensions, enable Memory Saver/Sleeping tabs."))
            exe = {"Google Chrome": "chrome.exe", "Microsoft Edge": "msedge.exe", "Mozilla Firefox": "firefox.exe", "Brave": "brave.exe"}.get(group["browser"])
            if exe:
                fixes.append(_fix("kill_process", {"name": exe}, f"Restart {group['browser']} to release memory (user loses unsaved form data)."))
            fixes.append(_fix("clean_junk", {"targets": ["browser_cache"]}, "Clear browser caches (browser should be closed first)."))

    top = results.get("top_processes") or {}
    if top.get("ram_used_percent", 0) >= 85:
        names = ", ".join(f"{p['name']} ({p['memory']})" for p in top.get("processes", [])[:3])
        findings.append(_finding("high", f"RAM is {top['ram_used_percent']}% used", f"Biggest users: {names}."))
    for process in top.get("processes", [])[:5]:
        if process.get("cpu_percent", 0) >= 50:
            findings.append(_finding("high", f"{process['name']} is using {process['cpu_percent']}% CPU", f"PID {process['pid']}."))

    spike = results.get("cpu_spike_watch") or {}
    for culprit in spike.get("culprits", [])[:3]:
        if culprit.get("seconds_above_25", 0) >= 3:
            findings.append(_finding("high" if culprit["max_cpu_percent"] >= 60 else "medium",
                                     f"CPU spikes caused by {culprit['name']}",
                                     f"Peaked at {culprit['max_cpu_percent']}% and stayed above 25% for {culprit['seconds_above_25']}s."))

    temps = results.get("temperatures") or {}
    hottest = max((sensor.get("current_c") or 0 for sensor in temps.get("sensors", [])), default=0)
    if hottest >= 90:
        findings.append(_finding("critical", f"CPU temperature is {hottest}°C", "Clean the vents/fan, check thermal paste, use on a hard surface."))
    if temps.get("clock_percent_of_max") and temps["clock_percent_of_max"] < 60 and temps.get("cpu_load_percent", 0) > 50:
        findings.append(_finding("medium", "CPU clock is throttled under load", f"Running at {temps['clock_percent_of_max']}% of max clock."))

    for volume in (results.get("disk_usage") or {}).get("volumes", []):
        if volume.get("percent", 0) >= 90:
            findings.append(_finding("critical", f"{volume['mountpoint']} is {volume['percent']}% full", f"Only {volume['free']} free."))
        elif volume.get("percent", 0) >= 80:
            findings.append(_finding("medium", f"{volume['mountpoint']} is {volume['percent']}% full", f"{volume['free']} free."))

    snapshot = results.get("storage_snapshot") or {}
    for item in (snapshot.get("growth") or [])[:5]:
        if item.get("grew_bytes", 0) >= 1024 ** 3:
            findings.append(_finding("high", f"{item['name']} grew by {item['grew']}", f"{item['path']} since the previous snapshot ({item.get('since', 'earlier')})."))

    cleanup = (results.get("cleanup_candidates") or {}).get("candidates", {})
    reclaimable = {name: info for name, info in cleanup.items() if info.get("bytes", 0) >= 500 * 1024 ** 2}
    if reclaimable:
        targets = []
        for name in reclaimable:
            targets.append({"temp_user": "temp", "temp_windows": "temp", "temp": "temp", "browser_cache": "browser_cache",
                            "update_cache": "update_cache", "crash_dumps": "crash_dumps", "trash": "recycle_bin",
                            "user_cache": "temp"}.get(name))
        targets = sorted({target for target in targets if target})
        detail = ", ".join(f"{name}: {info['size']}" for name, info in reclaimable.items())
        findings.append(_finding("medium", "Reclaimable junk found", detail))
        if targets:
            fixes.append(_fix("clean_junk", {"targets": targets}, f"Free space: {detail}."))

    folders = (results.get("largest_folders") or {}).get("folders", [])
    if folders:
        findings.append(_finding("low", "Largest folders", ", ".join(f"{f['path']} ({f['size']})" for f in folders[:4])))

    hangs = results.get("hang_and_crash_events") or {}
    if hangs.get("bsod_count"):
        findings.append(_finding("critical", f"{hangs['bsod_count']} blue-screen crash dump(s) found", "Check drivers, RAM (run stress_ram) and disk health."))
    categories = {}
    for event in hangs.get("events", []):
        categories[event.get("category", "Event")] = categories.get(event.get("category", "Event"), 0) + 1
    if categories.get("App hang"):
        findings.append(_finding("high", f"{categories['App hang']} application hang event(s)", "See hang_and_crash_events for which apps."))
    if categories.get("Unexpected shutdown/BSOD") or categories.get("Unexpected shutdown"):
        findings.append(_finding("high", "Unexpected shutdowns recorded", "Power/thermal problems or crashes."))
    if categories.get("WHEA error"):
        findings.append(_finding("critical", "Hardware (WHEA) errors recorded", "Possible CPU/RAM/board fault — run stress tests and check temperatures."))
    if hangs.get("oom_events"):
        findings.append(_finding("high", "Out-of-memory kills recorded", f"{len(hangs['oom_events'])} OOM events."))

    for process in (results.get("not_responding") or {}).get("not_responding", [])[:5]:
        name = process.get("ProcessName") or process.get("name")
        pid = process.get("Id") or process.get("pid")
        findings.append(_finding("high", f"{name} is not responding", f"PID {pid}."))
        if pid:
            fixes.append(_fix("kill_process", {"pid": int(pid)}, f"End the frozen {name} window."))

    for disk in (results.get("disk_health") or {}).get("disks", []):
        status = str(disk.get("HealthStatus") or disk.get("health") or "").lower()
        if status and status not in ("healthy", "passed", "verified", "ok"):
            findings.append(_finding("critical", f"Disk {disk.get('FriendlyName') or disk.get('device')} health: {status}", "Back up data now and plan a replacement."))
    for counter in (results.get("disk_health") or {}).get("reliability", []):
        if (counter.get("Wear") or 0) >= 80:
            findings.append(_finding("high", f"SSD wear at {counter['Wear']}%", "Plan a replacement."))

    drivers = results.get("driver_problems") or {}
    if drivers.get("count"):
        findings.append(_finding("medium", f"{drivers['count']} device(s) with driver problems", "; ".join(str(d.get("FriendlyName") or d.get("message", ""))[:60] for d in drivers.get("problem_devices", [])[:3])))

    integrity = results.get("os_integrity_check") or {}
    text = " ".join(str(check.get("output", "")) for check in integrity.get("checks", [])).lower()
    if "repairable" in text or "corrupt" in text or integrity.get("violations_found") or integrity.get("cbs_log_corruption_mentions"):
        findings.append(_finding("high", "Windows component store / system files report corruption", "Run DISM RestoreHealth, then SFC."))
        fixes.append(_fix("create_restore_point", {}, "Safety net before repairing."))
        fixes.append(_fix("repair_os", {"step": "dism_restorehealth"}, "Repair the component store from Windows Update."))
        fixes.append(_fix("repair_os", {"step": "sfc"}, "Then repair protected system files."))
    elif integrity.get("checks"):
        findings.append(_finding("low", "OS integrity quick check", "No corruption reported by the quick check (run with deep=true to be thorough)."))

    apps = results.get("installed_apps") or {}
    for app in apps.get("apps", [])[:10]:
        if app.get("orphaned") or app.get("hidden_from_control_panel"):
            why = ", ".join(app.get("orphan_reasons") or []) or "hidden from Control Panel"
            findings.append(_finding("medium", f"{app['name']} has a broken/hidden uninstall entry", why))
            if app.get("orphaned"):
                fixes.append(_fix("remove_app_entry", {"app_id": app["id"]}, f"Remove the orphaned entry for {app['name']} (backed up)."))
            else:
                fixes.append(_fix("uninstall_app", {"app_id": app["id"], "mode": "force"}, f"Force-uninstall {app['name']}."))
    search = apps.get("apps", [])
    if search and len(search) <= 5 and not any(app.get("orphaned") for app in search):
        for app in search:
            fixes.append(_fix("uninstall_app", {"app_id": app["id"], "mode": "standard"}, f"Uninstall {app['name']} (retry with mode=force if it fails)."))

    leftovers = results.get("uninstall_leftovers") or {}
    if leftovers.get("folders") or leftovers.get("services") or leftovers.get("scheduled_tasks"):
        findings.append(_finding("medium", f"Leftovers of {leftovers.get('name')} found",
                                 f"{len(leftovers.get('folders', []))} folders, {len(leftovers.get('services', []))} services, {len(leftovers.get('scheduled_tasks', []))} scheduled tasks."))

    updates = results.get("pending_updates") or {}
    if updates.get("pending_count"):
        findings.append(_finding("medium", f"{updates['pending_count']} update(s) pending", ", ".join(str(u.get("Title"))[:60] for u in updates.get("pending", [])[:3])))
        fixes.append(_fix("install_updates", {}, "Install pending updates."))
    if updates.get("reboot_required"):
        findings.append(_finding("medium", "A reboot is required to finish updates", ""))
        fixes.append(_fix("schedule_reboot", {"delay_seconds": 300}, "Finish pending updates (5-minute warning to the user)."))

    network = results.get("network_check") or {}
    if network:
        if network.get("gateway_ping") and not network["gateway_ping"].get("reachable"):
            findings.append(_finding("critical", "Cannot reach the router/gateway", "Check Wi-Fi/cable and adapter."))
            fixes.append(_fix("network_repair", {"actions": ["renew_ip"]}, "Renew the DHCP lease."))
        elif network.get("internet_ping") and not network["internet_ping"].get("reachable"):
            findings.append(_finding("high", "Router reachable but no internet", "ISP or firewall/proxy problem."))
        if network.get("dns") and not network["dns"].get("ok"):
            findings.append(_finding("high", "DNS resolution fails", network["dns"].get("error", "")))
            fixes.append(_fix("network_repair", {"actions": ["flush_dns"]}, "Flush the DNS cache."))

    battery = results.get("battery_health") or {}
    if battery.get("wear_percent") and battery["wear_percent"] >= 30:
        findings.append(_finding("high" if battery["wear_percent"] >= 40 else "medium", f"Battery wear is {battery['wear_percent']}%",
                                 f"{battery.get('cycle_count') or '?'} cycles. Consider replacing the battery."))

    startup = (results.get("startup_items") or {}).get("items", [])
    if len(startup) >= 10:
        findings.append(_finding("medium", f"{len(startup)} startup items", "Disable ones the user doesn't need to speed up logon."))

    security = results.get("security_status") or {}
    if security.get("firewall", {}).get("enabled") is False:
        findings.append(_finding("critical", "Firewall is disabled", ""))
    if security.get("antivirus", {}).get("enabled") is False:
        findings.append(_finding("critical", "Antivirus real-time protection is disabled", ""))

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda item: order.get(item["severity"], 4))
    unique, seen = [], set()
    for fix in fixes:
        key = (fix["tool"], str(sorted(fix["args"].items())))
        if key not in seen:
            seen.add(key)
            unique.append(fix)
    return {"findings": findings, "fixes": unique}

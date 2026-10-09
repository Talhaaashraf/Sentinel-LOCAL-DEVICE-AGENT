"""Built-in IT troubleshooting playbooks.

A playbook gives the agent a deterministic head start: its baseline tools run
before the LLM is consulted, so even a small local model starts from real
evidence instead of guessing which checks matter.
"""

ANY = None
WINDOWS = {"Windows"}

PLAYBOOKS = [
    {"id": "slow_pc", "title": "Slow / hanging PC", "issue": "The computer is slow, laggy or keeps hanging.",
     "tools": [("top_processes", {"sort_by": "cpu"}, ANY), ("top_processes", {"sort_by": "memory"}, ANY), ("startup_programs", {}, ANY)],
     "keywords": ["slow", "hang", "lag", "freeze", "frozen", "sluggish", "stuck", "not responding", "ahista", "atak"]},
    {"id": "no_internet", "title": "No / slow internet", "issue": "Internet is not working or is very slow.",
     "tools": [("check_internet", {}, ANY), ("wifi_status", {}, ANY)],
     "keywords": ["internet", "network", "website", "browser", "dns", "connect", "online", "offline", "ethernet", "lan"]},
    {"id": "wifi_drops", "title": "Wi-Fi keeps dropping", "issue": "Wi-Fi disconnects frequently or has a weak signal.",
     "tools": [("wifi_status", {}, ANY), ("check_internet", {}, ANY), ("event_log_errors", {}, ANY)],
     "keywords": ["wifi", "wi-fi", "wireless", "signal", "disconnect", "drops"]},
    {"id": "disk_full", "title": "Disk full", "issue": "The disk is full or running out of storage space.",
     "tools": [("cleanup_candidates", {}, ANY)],
     "keywords": ["disk", "storage", "space", "full", "drive", "low disk"]},
    {"id": "high_usage", "title": "High CPU / RAM", "issue": "CPU or memory usage is very high and the fan is loud.",
     "tools": [("top_processes", {"sort_by": "cpu"}, ANY), ("top_processes", {"sort_by": "memory"}, ANY)],
     "keywords": ["cpu", "ram", "memory", "fan", "hot", "heat", "100%", "usage"]},
    {"id": "printer", "title": "Printer not working", "issue": "The printer is not printing or jobs are stuck in the queue.",
     "tools": [("printer_status", {}, ANY), ("service_status", {"name": "Spooler"}, WINDOWS)],
     "keywords": ["print", "printer", "spooler", "queue"]},
    {"id": "windows_update", "title": "Updates stuck / failing", "issue": "Operating system updates are stuck, failing or not installing.",
     "tools": [("update_status", {}, ANY), ("service_status", {"name": "wuauserv"}, WINDOWS), ("event_log_errors", {}, ANY)],
     "keywords": ["update", "patch", "upgrade", "windows update"]},
    {"id": "crashes", "title": "Crashes / blue screen", "issue": "The computer or apps crash, restart unexpectedly or show a blue screen.",
     "tools": [("recent_crashes", {}, ANY), ("event_log_errors", {}, ANY), ("disk_health", {}, ANY)],
     "keywords": ["crash", "blue screen", "bsod", "restart", "reboot", "shutdown", "panic", "closes"]},
    {"id": "battery", "title": "Battery drains fast", "issue": "The battery drains quickly or does not hold charge.",
     "tools": [("battery_health", {}, ANY), ("top_processes", {"sort_by": "cpu"}, ANY)],
     "keywords": ["battery", "charge", "charging", "drain", "power"]},
    {"id": "audio", "title": "No sound", "issue": "There is no sound or the microphone does not work.",
     "tools": [("service_status", {"name": "Audiosrv"}, WINDOWS), ("service_status", {"name": "AudioEndpointBuilder"}, WINDOWS), ("event_log_errors", {}, ANY)],
     "keywords": ["sound", "audio", "speaker", "mic", "microphone", "headphone", "volume"]},
    {"id": "security", "title": "Security check", "issue": "Check this computer for security problems, malware signs or risky settings.",
     "tools": [("security_posture", {}, ANY), ("startup_programs", {}, ANY), ("top_processes", {"sort_by": "cpu"}, ANY)],
     "keywords": ["virus", "malware", "hack", "security", "firewall", "antivirus", "suspicious", "popup"]},
    {"id": "full_checkup", "title": "Full health check-up", "issue": "Run a full health check-up and report anything wrong.",
     "tools": [("top_processes", {"sort_by": "cpu"}, ANY), ("check_internet", {}, ANY), ("event_log_errors", {}, ANY), ("security_posture", {}, ANY), ("disk_health", {}, ANY)],
     "keywords": ["checkup", "check-up", "health check", "everything", "general"]},
]

GENERAL_TOOLS = [("event_log_errors", {}, ANY), ("top_processes", {"sort_by": "cpu"}, ANY)]

# Fixes that can plausibly address each playbook's problem. Small models like to
# propose unrelated fixes (e.g. renew_ip for a full disk); these lists filter them out.
RELEVANT_FIXES = {
    "slow_pc": {"kill_process", "disable_startup_item", "clear_temp_files", "restart_service", "repair_system_files"},
    "no_internet": {"flush_dns", "renew_ip", "reset_network_stack", "restart_service", "sync_time"},
    "wifi_drops": {"renew_ip", "flush_dns", "restart_service", "reset_network_stack"},
    "disk_full": {"clear_temp_files", "empty_recycle_bin", "clear_windows_update_cache"},
    "high_usage": {"kill_process", "disable_startup_item", "restart_service"},
    "printer": {"clear_print_queue", "restart_service"},
    "windows_update": {"clear_windows_update_cache", "trigger_update_scan", "restart_service", "sync_time", "repair_windows_image", "repair_system_files"},
    "crashes": {"repair_system_files", "repair_windows_image", "kill_process"},
    "battery": {"kill_process", "disable_startup_item"},
    "audio": {"restart_service"},
    "security": {"disable_startup_item", "kill_process", "trigger_update_scan"},
}


def filter_relevant_fixes(fixes, playbook):
    """Drop fixes unrelated to the matched playbook (learned fixes are always kept)."""
    allowed = RELEVANT_FIXES.get((playbook or {}).get("id"))
    if not allowed:
        return fixes
    return [fix for fix in fixes if fix["tool_id"] in allowed or fix.get("learned")]


def public_playbooks():
    return [{"id": item["id"], "title": item["title"], "issue": item["issue"]} for item in PLAYBOOKS]


def get_playbook(playbook_id):
    return next((item for item in PLAYBOOKS if item["id"] == playbook_id), None)


def match_playbook(issue):
    """Pick the playbook whose keywords best match a free-text issue (None if nothing matches)."""
    text = (issue or "").lower()
    best, best_score = None, 0
    for item in PLAYBOOKS:
        score = sum(len(keyword) for keyword in item["keywords"] if keyword in text)
        if score > best_score:
            best, best_score = item, score
    return best


def baseline_tools(playbook, os_type):
    """system_overview first, then the playbook's tools that apply to this OS, de-duplicated."""
    entries = [("system_overview", {}, ANY)] + (playbook["tools"] if playbook else GENERAL_TOOLS)
    seen, result = set(), []
    for tool_id, args, systems in entries:
        key = (tool_id, tuple(sorted(args.items())))
        if key in seen or (systems and os_type not in systems):
            continue
        seen.add(key)
        result.append((tool_id, args))
    return result

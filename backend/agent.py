"""Rule-based diagnostic agent with a swappable report-in/report-out interface."""

CPU_WARNING_PERCENT = 85
RAM_WARNING_PERCENT = 85
DISK_CRITICAL_PERCENT = 90
DISK_ADVISORY_PERCENT = 75
BATTERY_LOW_PERCENT = 15
CORE_VARIANCE_PERCENT = 45

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
ACTION_LABELS = {
    "list_top_processes": "Inspect top processes",
    "check_disk_cleanup_candidates": "Review disk cleanup candidates",
    "check_startup_programs": "Review startup programs",
}


class DiagnosticAgent:
    """Observe a report, apply deterministic rules, and prioritize issues."""

    def analyze(self, report):
        issues = []
        cpu = report.get("cpu", {})
        memory = report.get("memory", {}).get("ram", {})
        disks = report.get("disk", [])
        battery = report.get("battery")
        network = report.get("network", {})

        cpu_usage = cpu.get("total_usage_percent", 0)
        if cpu_usage > CPU_WARNING_PERCENT:
            issues.append(self._issue("high", "CPU", f"CPU usage is {cpu_usage:.1f}%, above the {CPU_WARNING_PERCENT}% threshold.", "list_top_processes"))

        ram_usage = memory.get("usage_percent", 0)
        if ram_usage > RAM_WARNING_PERCENT:
            issues.append(self._issue("high", "Memory", f"RAM usage is {ram_usage:.1f}%, above the {RAM_WARNING_PERCENT}% threshold.", "list_top_processes"))

        for disk in disks:
            usage = disk.get("usage_percent", 0)
            if usage > DISK_CRITICAL_PERCENT:
                issues.append(self._issue("critical", "Disk", f"{disk.get('mountpoint', 'A disk')} is {usage:.1f}% full.", "check_disk_cleanup_candidates"))
            elif usage > DISK_ADVISORY_PERCENT:
                issues.append(self._issue("medium", "Disk", f"{disk.get('mountpoint', 'A disk')} is {usage:.1f}% full and approaching capacity.", "check_disk_cleanup_candidates"))

        per_core = cpu.get("per_core_usage_percent", [])
        if len(per_core) > 1 and cpu.get("logical_cores", 0) >= 4:
            spread = max(per_core) - min(per_core)
            if spread > CORE_VARIANCE_PERCENT and max(per_core) > CPU_WARNING_PERCENT:
                issues.append(self._issue("medium", "CPU pattern", "CPU load varies sharply between cores, which can indicate a runaway single-threaded process.", "list_top_processes"))

        if battery and battery.get("percent", 100) < BATTERY_LOW_PERCENT and not battery.get("charging", False):
            issues.append(self._issue("high", "Battery", f"Battery is low at {battery['percent']:.1f}% and the device is not charging.", "check_startup_programs"))

        for interface in network.get("interfaces", []):
            if interface.get("status") == "DOWN" and interface.get("ipv4_addresses"):
                issues.append(self._issue("medium", "Network", f"Interface {interface.get('name', 'unknown')} is down but has a configured IPv4 address.", "check_startup_programs"))

        issues.sort(key=lambda issue: SEVERITY_ORDER[issue["severity"]])
        action_ids = []
        for issue in issues:
            action_id = issue["recommendation"]
            if action_id not in action_ids:
                action_ids.append(action_id)
        suggested_actions = [
            {
                "id": action_id,
                "label": ACTION_LABELS[action_id],
                "description": self._action_description(action_id),
            }
            for action_id in action_ids
        ]
        summary = self._summary(issues)
        return {"summary": summary, "issues": issues, "suggested_actions": suggested_actions}

    @staticmethod
    def _issue(severity, category, description, recommendation):
        return {"severity": severity, "category": category, "description": description, "recommendation": recommendation}

    @staticmethod
    def _action_description(action_id):
        descriptions = {
            "list_top_processes": "Read-only CPU and memory ranking of the five most active processes.",
            "check_disk_cleanup_candidates": "Inspect temporary-file usage and report whether recycle-bin data is available.",
            "check_startup_programs": "Read startup entries when the current platform exposes them safely.",
        }
        return descriptions[action_id]

    @staticmethod
    def _summary(issues):
        if not issues:
            return "The system looks healthy. I found no metrics above the configured warning thresholds."
        highest = issues[0]["severity"]
        return f"I found {len(issues)} issue(s), with {highest} priority first. The suggested read-only checks target the most likely sources of pressure."

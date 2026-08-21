"""Local threshold rules. This runs without AI and before any Groq request."""

from datetime import datetime, timezone

CPU_WARNING_PERCENT = 85
RAM_WARNING_PERCENT = 85
DISK_CRITICAL_PERCENT = 90
DISK_WARNING_PERCENT = 75
BATTERY_LOW_PERCENT = 15
CPU_SPIKE_PERCENT = 40
SPIKE_WINDOW_SECONDS = 10
AUTO_DIAGNOSE_SEVERITY = "high"
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
RISKY_PORTS = {21: "FTP (unencrypted)", 23: "Telnet (unencrypted)", 445: "SMB", 3389: "RDP", 5900: "VNC"}
LOOPBACK_PREFIXES = ("127.", "::1")


def _alert(severity, category, description):
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "severity": severity, "category": category, "description": description}


def evaluate_security_rules(security):
    if not security:
        return []
    alerts = []
    firewall = security.get("firewall", {})
    if firewall.get("available") and firewall.get("enabled") is False:
        alerts.append(_alert("critical", "Security", "Firewall is disabled."))
    antivirus = security.get("antivirus", {})
    if antivirus.get("available") and antivirus.get("enabled") is False:
        alerts.append(_alert("critical", "Security", "Antivirus / real-time protection is disabled."))
    encryption = security.get("disk_encryption", {})
    if encryption.get("available") and encryption.get("encrypted") is False:
        alerts.append(_alert("medium", "Security", "System drive is not encrypted."))
    for port_info in security.get("listening_ports", {}).get("ports", []):
        label = RISKY_PORTS.get(port_info.get("port"))
        address = str(port_info.get("address", ""))
        if label and not address.startswith(LOOPBACK_PREFIXES):
            alerts.append(_alert("high", "Security", f"{label} port {port_info['port']} is listening on {address} ({port_info.get('process', 'unknown process')})."))
    return sorted(alerts, key=lambda item: SEVERITY_ORDER[item["severity"]])


def evaluate_rules(report, previous_report=None):
    alerts = []
    cpu = report.get("cpu", {})
    ram = report.get("memory", {}).get("ram", {})
    if cpu.get("total_usage_percent", 0) > CPU_WARNING_PERCENT:
        alerts.append(_alert("high", "CPU", f"CPU usage is {cpu['total_usage_percent']:.1f}%, above {CPU_WARNING_PERCENT}%"))
    if ram.get("usage_percent", 0) > RAM_WARNING_PERCENT:
        alerts.append(_alert("high", "Memory", f"RAM usage is {ram['usage_percent']:.1f}%, above {RAM_WARNING_PERCENT}%"))
    for disk in report.get("disk", []):
        usage = disk.get("usage_percent", 0)
        if usage > DISK_CRITICAL_PERCENT:
            alerts.append(_alert("critical", "Disk", f"{disk.get('mountpoint', 'A disk')} is {usage:.1f}% full"))
        elif usage > DISK_WARNING_PERCENT:
            alerts.append(_alert("medium", "Disk", f"{disk.get('mountpoint', 'A disk')} is {usage:.1f}% full"))
    battery = report.get("battery")
    if battery and battery.get("percent", 100) < BATTERY_LOW_PERCENT and not battery.get("charging"):
        alerts.append(_alert("high", "Battery", f"Battery is low at {battery['percent']:.1f}% and not charging"))
    for interface in report.get("network", {}).get("interfaces", []):
        if interface.get("status") == "DOWN" and interface.get("ipv4_addresses"):
            alerts.append(_alert("medium", "Network", f"{interface['name']} is down but has a configured IP"))
    if previous_report:
        before = previous_report.get("cpu", {}).get("total_usage_percent", 0)
        after = cpu.get("total_usage_percent", 0)
        if after - before > CPU_SPIKE_PERCENT:
            alerts.append(_alert("high", "CPU spike", f"CPU jumped {after - before:.1f}% within the monitoring window"))
    return sorted(alerts, key=lambda item: SEVERITY_ORDER[item["severity"]])


def health_from_report(report):
    issues = []
    if report.get("cpu", {}).get("total_usage_percent", 0) > CPU_WARNING_PERCENT:
        issues.append(f"CPU usage is above {CPU_WARNING_PERCENT}%.")
    if report.get("memory", {}).get("ram", {}).get("usage_percent", 0) > RAM_WARNING_PERCENT:
        issues.append(f"RAM usage is above {RAM_WARNING_PERCENT}%.")
    if any(disk.get("usage_percent", 0) > DISK_CRITICAL_PERCENT for disk in report.get("disk", [])):
        issues.append(f"At least one disk is above {DISK_CRITICAL_PERCENT}% capacity.")
    critical = any("disk" in issue.lower() for issue in issues)
    return {"status": "critical" if critical else "warning" if issues else "ok", "message": "System health is within configured thresholds." if not issues else " ".join(issues), "issues": issues}

"""0-100 health score for a device from its latest report and recent alerts."""


def score(report, alerts=None):
    """Return {"score", "grade", "deductions": [{"reason", "points"}]}."""
    report = report or {}
    deductions = []

    def deduct(points, reason):
        deductions.append({"reason": reason, "points": points})

    cpu = report.get("cpu", {}).get("total_usage_percent", 0) or 0
    if cpu >= 90:
        deduct(15, f"CPU at {cpu:.0f}%")
    elif cpu >= 75:
        deduct(7, f"CPU at {cpu:.0f}%")

    ram = report.get("memory", {}).get("ram", {}).get("usage_percent", 0) or 0
    if ram >= 90:
        deduct(15, f"RAM at {ram:.0f}%")
    elif ram >= 80:
        deduct(7, f"RAM at {ram:.0f}%")

    worst_disk = max((disk.get("usage_percent", 0) or 0 for disk in report.get("disk", [])), default=0)
    if worst_disk >= 95:
        deduct(20, f"Disk {worst_disk:.0f}% full")
    elif worst_disk >= 85:
        deduct(10, f"Disk {worst_disk:.0f}% full")

    logs = report.get("event_logs") or {}
    if logs.get("critical_count"):
        deduct(min(15, 5 * logs["critical_count"]), f"{logs['critical_count']} critical OS log event(s)")
    if (logs.get("error_count") or 0) >= 10:
        deduct(5, f"{logs['error_count']} OS log errors")

    security = report.get("security") or {}
    if security.get("firewall", {}).get("enabled") is False:
        deduct(10, "Firewall disabled")
    if security.get("antivirus", {}).get("enabled") is False:
        deduct(10, "Antivirus disabled")

    battery = report.get("battery") or {}
    if battery and (battery.get("percent") or 100) < 15 and not battery.get("charging"):
        deduct(3, "Battery low")

    for alert in (alerts or [])[:20]:
        if alert.get("severity") == "critical":
            deduct(3, f"Recent alert: {alert.get('category')}")

    total = sum(item["points"] for item in deductions)
    value = max(0, 100 - total)
    grade = "good" if value >= 80 else "fair" if value >= 60 else "poor"
    return {"score": value, "grade": grade, "deductions": deductions}

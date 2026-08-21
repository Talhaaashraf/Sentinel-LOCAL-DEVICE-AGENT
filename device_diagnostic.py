"""Cross-platform device diagnostic tool powered by psutil.

Collects system, CPU, memory, disk, network, and optional battery details,
then presents a threshold-based health verdict as console output, JSON, or HTML.
"""

import argparse
import datetime
import json
import platform
import socket

import psutil



def human_size(value):
    """Convert a byte count to a readable KB/MB/GB/TB string."""
    if value is None:
        return "N/A"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"



def format_timestamp(timestamp):
    """Return a consistent local timestamp for a Unix timestamp."""
    return datetime.datetime.fromtimestamp(timestamp).isoformat(
        sep=" ", timespec="seconds"
    )



def format_duration(seconds):
    """Convert seconds into a compact uptime string."""
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)



def get_system_info():
    """Collect operating system and host details."""
    boot_time = psutil.boot_time()
    uptime_seconds = max(0, datetime.datetime.now().timestamp() - boot_time)
    return {
        "os_name": platform.system(),
        "os_version": platform.version(),
        "os_release": platform.release(),
        "machine_architecture": platform.machine(),
        "processor": platform.processor() or "Unknown",
        "hostname": socket.gethostname(),
        "boot_time": format_timestamp(boot_time),
        "uptime": format_duration(uptime_seconds),
        "uptime_seconds": round(uptime_seconds, 2),
    }



def get_cpu_info():
    """Collect processor counts, frequencies, and usage percentages."""
    frequency = psutil.cpu_freq()
    per_core_usage = psutil.cpu_percent(interval=0.1, percpu=True)
    return {
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "max_frequency_mhz": round(frequency.max, 2) if frequency else None,
        "current_frequency_mhz": round(frequency.current, 2) if frequency else None,
        "per_core_usage_percent": [round(value, 2) for value in per_core_usage],
        "total_usage_percent": round(psutil.cpu_percent(interval=None), 2),
    }



def get_memory_info():
    """Collect RAM and swap memory details."""
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "ram": {
            "total": human_size(memory.total),
            "available": human_size(memory.available),
            "used": human_size(memory.used),
            "usage_percent": round(memory.percent, 2),
        },
        "swap": {
            "total": human_size(swap.total),
            "used": human_size(swap.used),
            "usage_percent": round(swap.percent, 2),
        },
    }



def get_disk_info():
    """Collect usage details for accessible mounted partitions."""
    partitions = []
    for partition in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (PermissionError, OSError):
            continue
        partitions.append(
            {
                "device": partition.device,
                "mountpoint": partition.mountpoint,
                "filesystem": partition.fstype,
                "total": human_size(usage.total),
                "used": human_size(usage.used),
                "free": human_size(usage.free),
                "usage_percent": round(usage.percent, 2),
            }
        )
    return partitions



def get_network_info():
    """Collect traffic totals and interface status with IPv4 addresses."""
    counters = psutil.net_io_counters()
    interfaces = []
    for name, addresses in psutil.net_if_addrs().items():
        ipv4_addresses = [
            address.address
            for address in addresses
            if address.family == socket.AF_INET
        ]
        is_up = psutil.net_if_stats().get(name)
        interfaces.append(
            {
                "name": name,
                "status": "UP" if is_up and is_up.isup else "DOWN",
                "ipv4_addresses": ipv4_addresses,
            }
        )
    return {
        "bytes_sent": human_size(counters.bytes_sent) if counters else "N/A",
        "bytes_received": human_size(counters.bytes_recv) if counters else "N/A",
        "interfaces": interfaces,
    }



def get_battery_info():
    """Collect battery details when the device exposes a battery."""
    battery = psutil.sensors_battery()
    if battery is None:
        return None

    if battery.secsleft in (
        psutil.POWER_TIME_UNLIMITED,
        psutil.POWER_TIME_UNKNOWN,
    ) or battery.secsleft < 0:
        time_remaining = "Unknown"
    else:
        time_remaining = format_duration(battery.secsleft)
    return {
        "percent": round(battery.percent, 2),
        "charging": battery.power_plugged,
        "time_remaining": time_remaining,
    }



def get_health_verdict(cpu_info, memory_info, disk_info):
    """Evaluate CPU, RAM, and disk usage against warning thresholds."""
    issues = []
    if cpu_info["total_usage_percent"] > 85:
        issues.append("CPU usage is above 85%")
    if memory_info["ram"]["usage_percent"] > 85:
        issues.append("RAM usage is above 85%")
    full_disks = [
        disk["mountpoint"]
        for disk in disk_info
        if disk["usage_percent"] > 90
    ]
    if full_disks:
        issues.append("Disk usage is above 90%: " + ", ".join(full_disks))
    return {
        "status": "warning" if issues else "ok",
        "message": "System health warnings detected." if issues else "System healthy.",
        "issues": issues,
    }



def collect_report():
    """Collect all diagnostic sections into one report."""
    system = get_system_info()
    cpu = get_cpu_info()
    memory = get_memory_info()
    disks = get_disk_info()
    network = get_network_info()
    battery = get_battery_info()
    return {
        "system": system,
        "cpu": cpu,
        "memory": memory,
        "disk": disks,
        "network": network,
        "battery": battery,
        "health": get_health_verdict(cpu, memory, disks),
    }



def print_console_report(report):
    """Print a readable report for interactive use."""
    sections = [
        ("SYSTEM INFO", report["system"]),
        ("CPU INFO", report["cpu"]),
        ("MEMORY INFO", report["memory"]),
        ("DISK INFO", report["disk"]),
        ("NETWORK INFO", report["network"]),
        ("BATTERY INFO", report["battery"] or "Not available"),
        ("HEALTH VERDICT", report["health"]),
    ]
    print("DEVICE DIAGNOSTIC REPORT")
    print("=" * 26)
    for title, values in sections:
        print(f"\n{title}\n" + "-" * len(title))
        print(json.dumps(values, indent=2))



def html_escape(value):
    """Escape values used in the generated HTML without extra dependencies."""
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )



def html_rows(value, prefix=""):
    """Render nested dictionaries and lists as table rows."""
    rows = []
    if isinstance(value, dict):
        for key, nested_value in value.items():
            rows.extend(html_rows(nested_value, f"{prefix}{key}."))
    elif isinstance(value, list):
        for index, nested_value in enumerate(value):
            rows.extend(html_rows(nested_value, f"{prefix}{index}."))
    else:
        rows.append(
            f"<tr><th>{html_escape(prefix.rstrip('.'))}</th>"
            f"<td>{html_escape(value)}</td></tr>"
        )
    return rows



def generate_html_report(report, filename="diagnostic_report.html"):
    """Write a styled HTML report to disk."""
    sections = [
        ("System Info", report["system"]),
        ("CPU Info", report["cpu"]),
        ("Memory Info", report["memory"]),
        ("Disk Info", report["disk"]),
        ("Network Info", report["network"]),
        ("Battery Info", report["battery"] or {"status": "Not available"}),
    ]
    section_html = []
    for title, values in sections:
        rows = "".join(html_rows(values))
        section_html.append(
            f"<section><h2>{html_escape(title)}</h2>"
            f"<table><tbody>{rows}</tbody></table></section>"
        )
    health = report["health"]
    health_class = "warning" if health["status"] == "warning" else "ok"
    health_html = (
        f'<section class="verdict {health_class}"><h2>Health Verdict</h2>'
        f"<p>{html_escape(health['message'])}</p>"
        f"<ul>{''.join(f'<li>{html_escape(issue)}</li>' for issue in health['issues'])}</ul>"
        "</section>"
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Device Diagnostic Report</title>
<style>
:root {{ color-scheme: dark; background: #10151c; color: #e8edf2; font-family: Consolas, monospace; }}
body {{ max-width: 1100px; margin: 0 auto; padding: 32px; background: #10151c; }}
h1 {{ color: #8bd5ca; letter-spacing: 1px; }}
section {{ margin: 24px 0; border: 1px solid #303b47; background: #18212b; }}
h2 {{ margin: 0; padding: 12px 16px; color: #ffd166; background: #202c38; font-size: 1.1rem; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ padding: 9px 16px; text-align: left; border-top: 1px solid #303b47; vertical-align: top; word-break: break-word; }}
th {{ width: 35%; color: #a9b8c6; font-weight: normal; }}
.verdict {{ padding-bottom: 12px; }}
.verdict p, .verdict ul {{ margin: 14px 16px; }}
.ok {{ border-color: #4dbb86; }} .ok h2 {{ color: #7ee2a8; }}
.warning {{ border-color: #e3aa46; }} .warning h2 {{ color: #ffd166; }}
</style>
</head>
<body><h1>Device Diagnostic Report</h1>{''.join(section_html)}{health_html}</body>
</html>
"""
    with open(filename, "w", encoding="utf-8") as report_file:
        report_file.write(document)


def main():
    """Parse CLI arguments and render the collected report."""
    parser = argparse.ArgumentParser(description="Cross-platform device diagnostics")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", help="print JSON output")
    output_group.add_argument(
        "--html", action="store_true", help="write diagnostic_report.html"
    )
    args = parser.parse_args()
    report = collect_report()
    if args.json:
        print(json.dumps(report, indent=2))
    elif args.html:
        generate_html_report(report)
        print("HTML report written to diagnostic_report.html")
    else:
        print_console_report(report)


if __name__ == "__main__":
    main()

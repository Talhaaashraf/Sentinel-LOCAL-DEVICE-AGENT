"""Time-series history of device metrics and stress runs.

Sentinel's device_reports table only keeps the last ~20 snapshots per device, which
is enough for "now" but not for trends. This module keeps a compact, pruned
time-series (CPU / RAM / disk / temperature / health score) sampled from every
report, plus a log of completed stress/benchmark runs, so the dashboard can chart
how a laptop behaves over days.
"""

import json
import time
from datetime import datetime, timezone

from .device_store import _connect as _device_connect
from .health_score import score as health_of

RETENTION_DAYS = 14
MIN_SAMPLE_GAP_SECONDS = 25  # don't store more than ~1 point per 25s per device


def _connect():
    connection = _device_connect()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS metric_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            ts REAL NOT NULL,
            cpu REAL, ram REAL, disk REAL, temp REAL, health INTEGER
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_metric_history ON metric_history(device_id, ts)")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS stress_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            ts REAL NOT NULL,
            test TEXT NOT NULL,
            summary TEXT,
            result_json TEXT
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_stress_history ON stress_history(device_id, ts)")
    return connection


def _max_temp(diagnostics):
    temps = [entry["current_c"] for entry in (diagnostics.get("temperatures") or {}).get("sensors", []) if entry.get("current_c")]
    return max(temps) if temps else None


def record_report(device_id, diagnostics):
    """Sample one point from a device report. Rate-limited and auto-pruned."""
    if not isinstance(diagnostics, dict):
        return
    now = time.time()
    with _connect() as connection:
        last = connection.execute(
            "SELECT ts FROM metric_history WHERE device_id = ? ORDER BY ts DESC LIMIT 1", (device_id,)
        ).fetchone()
        if last and now - last["ts"] < MIN_SAMPLE_GAP_SECONDS:
            return
        cpu = (diagnostics.get("cpu") or {}).get("total_usage_percent")
        ram = ((diagnostics.get("memory") or {}).get("ram") or {}).get("usage_percent")
        disk = max((d.get("usage_percent", 0) or 0 for d in diagnostics.get("disk", [])), default=None)
        temp = _max_temp(diagnostics)
        try:
            health = health_of(diagnostics).get("score")
        except Exception:
            health = None
        connection.execute(
            "INSERT INTO metric_history (device_id, ts, cpu, ram, disk, temp, health) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (device_id, now, cpu, ram, disk, temp, health),
        )
        connection.execute("DELETE FROM metric_history WHERE ts < ?", (now - RETENTION_DAYS * 86400,))
        connection.commit()


def record_stress(device_id, test, result):
    if not isinstance(result, dict):
        return
    summary = _stress_summary(test, result)
    with _connect() as connection:
        connection.execute(
            "INSERT INTO stress_history (device_id, ts, test, summary, result_json) VALUES (?, ?, ?, ?, ?)",
            (device_id, time.time(), test, summary, json.dumps(result)[:8000]),
        )
        connection.commit()


def _stress_summary(test, r):
    if test == "stress_cpu":
        return f"avg {r.get('avg_cpu_percent','?')}% CPU, max {r.get('max_temp_c','?')}°C, {'throttled' if r.get('throttling_suspected') else 'stable'}"
    if test == "stress_ram":
        return f"{r.get('allocated','?')} tested, {r.get('verify_mismatches','?')} errors, {'healthy' if r.get('healthy') else 'check RAM'}"
    if test == "stress_disk":
        return f"write {r.get('write_mbps','?')} MB/s, read {r.get('read_mbps','?')} MB/s, {r.get('random_read_iops','?')} IOPS"
    if test == "network_speed":
        return f"down {r.get('download_mbps','?')} Mbps, up {r.get('upload_mbps','?')} Mbps"
    return r.get("stopped_reason", "done")


def history(device_id, hours=24, buckets=120):
    """Return downsampled series for charting: evenly-spaced buckets with averaged values."""
    since = time.time() - hours * 3600
    with _connect() as connection:
        rows = connection.execute(
            "SELECT ts, cpu, ram, disk, temp, health FROM metric_history WHERE device_id = ? AND ts >= ? ORDER BY ts",
            (device_id, since),
        ).fetchall()
    points = [dict(r) for r in rows]
    if not points:
        return {"hours": hours, "points": 0, "series": {"cpu": [], "ram": [], "disk": [], "temp": [], "health": []}, "t": []}
    # Downsample into <= buckets groups.
    span = max(1.0, points[-1]["ts"] - points[0]["ts"])
    width = span / buckets
    grouped = {}
    for p in points:
        key = int((p["ts"] - points[0]["ts"]) // width) if width else 0
        grouped.setdefault(key, []).append(p)
    t, series = [], {k: [] for k in ("cpu", "ram", "disk", "temp", "health")}
    for key in sorted(grouped):
        group = grouped[key]
        t.append(datetime.fromtimestamp(group[0]["ts"], timezone.utc).isoformat())
        for metric in series:
            vals = [g[metric] for g in group if g[metric] is not None]
            series[metric].append(round(sum(vals) / len(vals), 1) if vals else None)
    return {"hours": hours, "points": len(points), "t": t, "series": series}


def stress_runs(device_id, limit=50):
    with _connect() as connection:
        rows = connection.execute(
            "SELECT ts, test, summary FROM stress_history WHERE device_id = ? ORDER BY ts DESC LIMIT ?",
            (device_id, limit),
        ).fetchall()
    return [{"when": datetime.fromtimestamp(r["ts"], timezone.utc).isoformat(), "test": r["test"], "summary": r["summary"]} for r in rows]

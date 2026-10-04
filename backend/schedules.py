"""Scheduled automatic health-checks.

A schedule runs a playbook preset's read-only tools against a device on an
interval; the async loop in main.py evaluates the findings and raises alerts.
Stored in the shared SQLite database next to devices and commands.
"""

import uuid
from datetime import datetime, timedelta, timezone

from .device_store import _connect as _device_connect


def utc_now():
    return datetime.now(timezone.utc)


def _connect():
    connection = _device_connect()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_checks (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            preset TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_run TEXT,
            last_result TEXT,
            next_run TEXT NOT NULL
        )
    """)
    connection.commit()
    return connection


def _decode(row):
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    return item


def create(device_id, preset, interval_minutes):
    interval_minutes = max(5, int(interval_minutes))
    schedule_id = f"sch-{uuid.uuid4().hex[:10]}"
    now = utc_now()
    next_run = (now + timedelta(minutes=interval_minutes)).isoformat()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO scheduled_checks (id, device_id, preset, interval_minutes, enabled, created_at, next_run) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (schedule_id, device_id, preset, interval_minutes, now.isoformat(), next_run),
        )
        connection.commit()
    return get(schedule_id)


def get(schedule_id):
    with _connect() as connection:
        row = connection.execute("SELECT * FROM scheduled_checks WHERE id = ?", (schedule_id,)).fetchone()
        return _decode(row) if row else None


def list_all(device_id=None):
    query, params = "SELECT * FROM scheduled_checks", []
    if device_id:
        query += " WHERE device_id = ?"
        params.append(device_id)
    query += " ORDER BY created_at DESC"
    with _connect() as connection:
        return [_decode(row) for row in connection.execute(query, params).fetchall()]


def set_enabled(schedule_id, enabled):
    with _connect() as connection:
        connection.execute("UPDATE scheduled_checks SET enabled = ? WHERE id = ?", (1 if enabled else 0, schedule_id))
        connection.commit()
    return get(schedule_id)


def delete(schedule_id):
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM scheduled_checks WHERE id = ?", (schedule_id,))
        connection.commit()
        return cursor.rowcount > 0


def due(now=None):
    now = (now or utc_now()).isoformat()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM scheduled_checks WHERE enabled = 1 AND next_run <= ?", (now,)
        ).fetchall()
        return [_decode(row) for row in rows]


def mark_ran(schedule_id, summary):
    now = utc_now()
    schedule = get(schedule_id)
    interval = schedule["interval_minutes"] if schedule else 60
    next_run = (now + timedelta(minutes=interval)).isoformat()
    with _connect() as connection:
        connection.execute(
            "UPDATE scheduled_checks SET last_run = ?, last_result = ?, next_run = ? WHERE id = ?",
            (now.isoformat(), summary, next_run, schedule_id),
        )
        connection.commit()

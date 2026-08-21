"""Persistent SQLite storage for local diagnostic alerts."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "alerts.db"


def _connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            severity TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            device_id TEXT NOT NULL DEFAULT 'local-server'
        )
    """)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(alerts)").fetchall()}
    if "device_id" not in columns:
        connection.execute("ALTER TABLE alerts ADD COLUMN device_id TEXT NOT NULL DEFAULT 'local-server'")
    connection.commit()
    return connection


def add_alert(alert, device_id="local-server"):
    timestamp = alert.get("timestamp") or datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        recent = connection.execute(
            "SELECT id FROM alerts WHERE device_id = ? AND category = ? AND description = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (device_id, alert["category"], alert["description"]),
        ).fetchone()
        if recent:
            return dict(recent)

        cursor = connection.execute(
            "INSERT INTO alerts (timestamp, severity, category, description, status, device_id) VALUES (?, ?, ?, ?, 'open', ?)",
            (timestamp, alert["severity"], alert["category"], alert["description"], device_id),
        )
        connection.commit()
        return {"id": cursor.lastrowid, **alert, "timestamp": timestamp, "status": "open", "device_id": device_id}


def get_alerts(limit=100, device_id=None):
    with _connect() as connection:
        if device_id:
            rows = connection.execute(
                "SELECT id, timestamp, severity, category, description, status, device_id FROM alerts WHERE device_id = ? ORDER BY id DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT id, timestamp, severity, category, description, status, device_id FROM alerts ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

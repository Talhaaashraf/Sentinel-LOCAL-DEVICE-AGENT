"""Audit log: who did what, for accountability on a tool that changes real machines."""

from datetime import datetime, timezone

from .device_store import _connect as _device_connect


def _connect():
    connection = _device_connect()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            username TEXT,
            action TEXT NOT NULL,
            target TEXT,
            detail TEXT
        )
    """)
    connection.commit()
    return connection


def record(username, action, target="", detail=""):
    try:
        with _connect() as connection:
            connection.execute(
                "INSERT INTO audit_log (ts, username, action, target, detail) VALUES (?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), username or "?", action, str(target)[:300], str(detail)[:500]),
            )
            connection.commit()
    except Exception:
        pass  # auditing must never break the action it records


def recent(limit=200):
    with _connect() as connection:
        rows = connection.execute(
            "SELECT ts, username, action, target, detail FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

"""SQLite queue of tool commands sent to agents, with the approval gate.

Lifecycle:
    read tools:              queued -> running -> done | error | rejected
    change/stress/shell:     pending_approval -> (approve) queued -> running -> ...
                             pending_approval -> (deny) denied
    any queued/running:      -> cancelled (technician cancel)

The agent only ever receives commands in the queued state, so nothing that
changes a laptop can run until a technician has approved it.
"""

import json
import uuid
from datetime import datetime, timezone

from .device_store import _connect as _device_connect

ACTIVE_STATES = ("pending_approval", "queued", "running")
FINAL_STATES = ("done", "error", "rejected", "denied", "cancelled")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _connect():
    connection = _device_connect()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_commands (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            tool TEXT NOT NULL,
            kind TEXT NOT NULL,
            args_json TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            reason TEXT,
            session_id TEXT,
            created_at TEXT NOT NULL,
            approved_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            progress_json TEXT,
            result_json TEXT,
            error TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_commands_device ON agent_commands(device_id, status)")
    connection.commit()
    return connection


def _decode(row):
    if not row:
        return None
    item = dict(row)
    item["args"] = json.loads(item.pop("args_json") or "{}")
    item["progress"] = json.loads(item.pop("progress_json") or "null")
    item["result"] = json.loads(item.pop("result_json") or "null")
    item["cancel_requested"] = bool(item["cancel_requested"])
    return item


def create_command(device_id, tool, kind, args, requested_by, needs_approval, reason=None, session_id=None):
    command_id = f"cmd-{uuid.uuid4().hex[:12]}"
    status = "pending_approval" if needs_approval else "queued"
    with _connect() as connection:
        connection.execute(
            "INSERT INTO agent_commands (id, device_id, tool, kind, args_json, status, requested_by, reason, session_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (command_id, device_id, tool, kind, json.dumps(args), status, requested_by, reason, session_id, utc_now()),
        )
        connection.commit()
    return get_command(command_id)


def get_command(command_id):
    with _connect() as connection:
        return _decode(connection.execute("SELECT * FROM agent_commands WHERE id = ?", (command_id,)).fetchone())


def list_commands(device_id=None, session_id=None, status=None, limit=100):
    query, params = "SELECT * FROM agent_commands WHERE 1=1", []
    if device_id:
        query += " AND device_id = ?"
        params.append(device_id)
    if session_id:
        query += " AND session_id = ?"
        params.append(session_id)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _connect() as connection:
        return [_decode(row) for row in connection.execute(query, params).fetchall()]


def approve(command_id):
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE agent_commands SET status = 'queued', approved_at = ? WHERE id = ? AND status = 'pending_approval'",
            (utc_now(), command_id),
        )
        connection.commit()
        return cursor.rowcount > 0


def deny(command_id):
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE agent_commands SET status = 'denied', finished_at = ? WHERE id = ? AND status = 'pending_approval'",
            (utc_now(), command_id),
        )
        connection.commit()
        return cursor.rowcount > 0


def cancel(command_id):
    """Queued commands are cancelled immediately; running ones are flagged for the agent."""
    with _connect() as connection:
        row = connection.execute("SELECT status FROM agent_commands WHERE id = ?", (command_id,)).fetchone()
        if not row:
            return None
        if row["status"] in ("queued", "pending_approval"):
            connection.execute("UPDATE agent_commands SET status = 'cancelled', finished_at = ? WHERE id = ?", (utc_now(), command_id))
        elif row["status"] == "running":
            connection.execute("UPDATE agent_commands SET cancel_requested = 1 WHERE id = ?", (command_id,))
        else:
            return False
        connection.commit()
        return True


def claim_next(device_id, limit=5):
    """Hand queued commands to the agent (marking them running) plus any cancel requests."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM agent_commands WHERE device_id = ? AND status = 'queued' ORDER BY created_at LIMIT ?",
            (device_id, limit),
        ).fetchall()
        now = utc_now()
        for row in rows:
            connection.execute("UPDATE agent_commands SET status = 'running', started_at = ? WHERE id = ?", (now, row["id"]))
        cancels = connection.execute(
            "SELECT id FROM agent_commands WHERE device_id = ? AND status = 'running' AND cancel_requested = 1",
            (device_id,),
        ).fetchall()
        connection.commit()
    commands = [{"id": row["id"], "tool": row["tool"], "args": json.loads(row["args_json"])} for row in rows]
    commands += [{"id": row["id"], "action": "cancel"} for row in cancels]
    return commands


def record_progress(command_id, device_id, progress):
    with _connect() as connection:
        connection.execute(
            "UPDATE agent_commands SET progress_json = ? WHERE id = ? AND device_id = ? AND status = 'running'",
            (json.dumps(progress), command_id, device_id),
        )
        connection.commit()


def record_result(command_id, device_id, status, result=None, error=None):
    if status not in ("done", "error", "rejected"):
        status = "error"
    with _connect() as connection:
        row = connection.execute("SELECT cancel_requested FROM agent_commands WHERE id = ? AND device_id = ?", (command_id, device_id)).fetchone()
        if not row:
            return False
        if row["cancel_requested"] and status == "done":
            status = "cancelled"
        connection.execute(
            "UPDATE agent_commands SET status = ?, result_json = ?, error = ?, finished_at = ? WHERE id = ? AND device_id = ?",
            (status, json.dumps(result) if result is not None else None, error, utc_now(), command_id, device_id),
        )
        connection.commit()
        return True


def latest_result(device_id, tool, before_id=None):
    """Most recent successful result of a tool on a device (used for storage-growth comparison)."""
    query = "SELECT * FROM agent_commands WHERE device_id = ? AND tool = ? AND status = 'done'"
    params = [device_id, tool]
    if before_id:
        query += " AND id != ?"
        params.append(before_id)
    query += " ORDER BY finished_at DESC LIMIT 1"
    with _connect() as connection:
        return _decode(connection.execute(query, params).fetchone())

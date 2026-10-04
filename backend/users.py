"""User accounts and roles for the dashboard.

Replaces the single shared password with per-technician accounts. Three roles:
  admin      - everything, including user management and the audit log
  technician - diagnose and run approved change/stress actions
  viewer     - read-only; cannot run anything that changes a device

Passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib, no extra deps). On first
run, if the table is empty, an admin is seeded from ADMIN_USERNAME/ADMIN_PASSWORD
(falling back to 'admin' + DASHBOARD_PASSWORD/ADMIN_API_KEY) so existing
single-password deployments keep working.
"""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone

from .device_store import _connect as _device_connect

ROLES = ("admin", "technician", "viewer")
CHANGE_ROLES = ("admin", "technician")  # may run change/stress actions
_PBKDF2_ROUNDS = 200_000


def _connect():
    connection = _device_connect()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'technician',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login TEXT
        )
    """)
    connection.commit()
    return connection


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ROUNDS).hex()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def count():
    with _connect() as connection:
        return connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def create_user(username, password, role="technician"):
    username = (username or "").strip().lower()
    if not username or not password:
        raise ValueError("username and password are required")
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    salt = secrets.token_hex(16)
    with _connect() as connection:
        if connection.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise ValueError("user already exists")
        connection.execute(
            "INSERT INTO users (username, password_hash, salt, role, active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (username, _hash(password, salt), salt, role, utc_now()),
        )
        connection.commit()
    return get_user(username)


def get_user(username):
    with _connect() as connection:
        row = connection.execute(
            "SELECT username, role, active, created_at, last_login FROM users WHERE username = ?",
            ((username or "").strip().lower(),),
        ).fetchone()
        return dict(row) if row else None


def list_users():
    with _connect() as connection:
        return [dict(r) for r in connection.execute(
            "SELECT username, role, active, created_at, last_login FROM users ORDER BY username").fetchall()]


def authenticate(username, password):
    username = (username or "").strip().lower()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
        if not row:
            return None
        expected = row["password_hash"]
        if not hmac.compare_digest(expected, _hash(password or "", row["salt"])):
            return None
        connection.execute("UPDATE users SET last_login = ? WHERE username = ?", (utc_now(), username))
        connection.commit()
        return {"username": row["username"], "role": row["role"]}


def set_password(username, password):
    if len(password or "") < 6:
        raise ValueError("password must be at least 6 characters")
    salt = secrets.token_hex(16)
    with _connect() as connection:
        cursor = connection.execute("UPDATE users SET password_hash = ?, salt = ? WHERE username = ?",
                                    (_hash(password, salt), salt, (username or "").strip().lower()))
        connection.commit()
        return cursor.rowcount > 0


def set_role(username, role):
    if role not in ROLES:
        raise ValueError("invalid role")
    with _connect() as connection:
        connection.execute("UPDATE users SET role = ? WHERE username = ?", (role, (username or "").strip().lower()))
        connection.commit()


def set_active(username, active):
    with _connect() as connection:
        connection.execute("UPDATE users SET active = ? WHERE username = ?", (1 if active else 0, (username or "").strip().lower()))
        connection.commit()


def delete_user(username):
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM users WHERE username = ?", ((username or "").strip().lower(),))
        connection.commit()
        return cursor.rowcount > 0


def ensure_seed_admin():
    """Seed an initial admin so a fresh/upgraded deployment can log in."""
    if count() > 0:
        return
    username = os.getenv("ADMIN_USERNAME", "admin").strip().lower() or "admin"
    password = (os.getenv("ADMIN_PASSWORD", "").strip()
                or os.getenv("DASHBOARD_PASSWORD", "").strip()
                or os.getenv("ADMIN_API_KEY", "").strip())
    if not password:
        return  # no credential available; multi-user auth stays off until one is set
    try:
        create_user(username, password, "admin")
    except ValueError:
        pass

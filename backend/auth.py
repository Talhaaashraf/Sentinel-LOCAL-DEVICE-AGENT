"""Shared-password session auth for the dashboard.

This is a single-operator local tool, not a multi-user system, so there is
no user table — just one shared password gating the whole dashboard. When
DASHBOARD_PASSWORD (or, as a fallback, ADMIN_API_KEY) is set in .env, a
signed session cookie is required to reach the UI and its APIs. When neither
is set, the dashboard stays open exactly as it always has, so a fresh clone
still works out of the box.
"""

import hashlib
import hmac
import os

SESSION_COOKIE_NAME = "sentinel_session"


def _dashboard_password():
    return os.getenv("DASHBOARD_PASSWORD", "").strip() or os.getenv("ADMIN_API_KEY", "").strip()


def auth_enabled():
    return bool(_dashboard_password())


def session_token():
    password = _dashboard_password()
    return hmac.new(password.encode("utf-8"), b"sentinel-dashboard-session", hashlib.sha256).hexdigest()


def check_password(candidate):
    password = _dashboard_password()
    return bool(password) and hmac.compare_digest(candidate.strip(), password)


def has_valid_session(cookies):
    if not auth_enabled():
        return True
    return hmac.compare_digest(cookies.get(SESSION_COOKIE_NAME, ""), session_token())


# Agent-facing endpoints authenticate with their own agent/enrollment token, not the
# dashboard cookie, so the session gate must let them through.
PUBLIC_PATH_PREFIXES = (
    "/login", "/logout", "/static",
    "/install/",
    "/api/agents/register", "/api/agents/report", "/api/agents/tasks/",
    "/api/agents/bundle", "/api/agents/binary/",
)

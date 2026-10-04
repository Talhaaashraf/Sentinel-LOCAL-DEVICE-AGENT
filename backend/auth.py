"""Session auth for the dashboard.

Primary mode is per-user accounts (see users.py): login issues a signed, stateless
cookie carrying the username, role and expiry. The legacy single shared password
(DASHBOARD_PASSWORD / ADMIN_API_KEY) still works — on first login it is accepted and
the seeded 'admin' account is used — so existing deployments keep working.

When neither any user nor a shared password is configured, the dashboard stays open
with no login, exactly as the original tool did out of the box.
"""

import base64
import hashlib
import hmac
import json
import os
import time

from . import users

SESSION_COOKIE_NAME = "sentinel_session"
_MAX_AGE = 60 * 60 * 24 * 30
PUBLIC_PATH_PREFIXES = ("/login", "/logout", "/static", "/install.ps1", "/install.sh",
                        "/agent-bundle.zip", "/api/agents/register", "/api/agents/report",
                        "/api/agents/commands", "/api/agents/speedtest")


def _secret():
    base = (os.getenv("SESSION_SECRET", "").strip()
            or os.getenv("DASHBOARD_PASSWORD", "").strip()
            or os.getenv("ADMIN_API_KEY", "").strip()
            or "sentinel-insecure-default-secret")
    return hashlib.sha256(("sentinel-session::" + base).encode("utf-8")).digest()


def _shared_password():
    return os.getenv("DASHBOARD_PASSWORD", "").strip() or os.getenv("ADMIN_API_KEY", "").strip()


def auth_enabled():
    """Login required when any account exists or a shared password is set."""
    try:
        if users.count() > 0:
            return True
    except Exception:
        pass
    return bool(_shared_password())


# ============================================================================
# Signed cookie
# ============================================================================

def issue_session(username, role):
    payload = {"u": username, "r": role, "exp": int(time.time()) + _MAX_AGE}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    sig = hmac.new(_secret(), raw.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def _decode(token):
    if not token or "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    expected = hmac.new(_secret(), raw.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def current_user(cookies):
    """Return {'username','role'} for a valid session cookie, else None."""
    payload = _decode(cookies.get(SESSION_COOKIE_NAME, ""))
    if not payload:
        return None
    return {"username": payload.get("u"), "role": payload.get("r", "viewer")}


def has_valid_session(cookies):
    if not auth_enabled():
        return True
    return current_user(cookies) is not None


# ============================================================================
# Login
# ============================================================================

def login(username, password):
    """Return a session cookie value on success, else None.

    Accepts a real account (username+password). If no username is given, falls back
    to the shared password and logs in as the seeded admin — preserving the old
    single-field login.
    """
    username = (username or "").strip().lower()
    if username:
        user = users.authenticate(username, password)
        if user:
            return issue_session(user["username"], user["role"])
        return None
    # Legacy shared-password path (no username supplied).
    shared = _shared_password()
    if shared and hmac.compare_digest((password or "").strip(), shared):
        users.ensure_seed_admin()
        admin = os.getenv("ADMIN_USERNAME", "admin").strip().lower() or "admin"
        role = (users.get_user(admin) or {}).get("role", "admin")
        return issue_session(admin, role)
    return None


# -- Back-compat shims (older callers) ---------------------------------------
def check_password(candidate):
    return login("", candidate) is not None


def session_token():
    return issue_session(os.getenv("ADMIN_USERNAME", "admin").strip().lower() or "admin", "admin")

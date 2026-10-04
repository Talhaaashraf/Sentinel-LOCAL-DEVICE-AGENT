"""Outbound notifications for alerts, plus a daily fleet summary.

Delivers critical/high alerts to email (SMTP) and/or a webhook (Slack/Teams/generic
JSON) when configured via environment variables. Everything is best-effort and runs
off the event loop in a thread, so a slow or misconfigured channel never blocks
diagnostics. If nothing is configured, this module is a no-op and the dashboard keeps
working exactly as before.

Config (.env):
  ALERT_MIN_SEVERITY   critical|high|medium|low   (default: critical)
  ALERT_WEBHOOK_URL    Slack/Teams/generic incoming webhook
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, ALERT_EMAIL_TO
  SUMMARY_ENABLED      true|false  (daily fleet summary; default false)
"""

import logging
import os
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage

import httpx

logger = logging.getLogger(__name__)
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_recent = {}            # (device, category, description) -> last-sent epoch
_COOLDOWN_SECONDS = 1800


def _min_rank():
    return SEVERITY_RANK.get(os.getenv("ALERT_MIN_SEVERITY", "critical").strip().lower(), 0)


def email_configured():
    return all(os.getenv(k, "").strip() for k in ("SMTP_HOST", "SMTP_FROM", "ALERT_EMAIL_TO"))


def webhook_configured():
    return bool(os.getenv("ALERT_WEBHOOK_URL", "").strip())


def enabled():
    return email_configured() or webhook_configured()


def status():
    return {
        "enabled": enabled(),
        "email": email_configured(),
        "webhook": webhook_configured(),
        "min_severity": os.getenv("ALERT_MIN_SEVERITY", "critical").strip().lower(),
        "daily_summary": os.getenv("SUMMARY_ENABLED", "").strip().lower() in ("1", "true", "yes"),
    }


# ============================================================================
# Senders
# ============================================================================

def _send_webhook(text):
    url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return False, "no webhook"
    try:
        # {"text": ...} is accepted by Slack and Teams incoming webhooks and is a
        # reasonable generic shape.
        httpx.post(url, json={"text": text}, timeout=10).raise_for_status()
        return True, "sent"
    except httpx.HTTPError as error:
        logger.warning("webhook notify failed: %s", error)
        return False, str(error)


def _send_email(subject, body):
    if not email_configured():
        return False, "email not configured"
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587") or 587)
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("SMTP_FROM", "").strip()
    recipients = [a.strip() for a in os.getenv("ALERT_EMAIL_TO", "").split(",") if a.strip()]
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context()) as server:
                if user:
                    server.login(user, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                try:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                except smtplib.SMTPException:
                    pass
                if user:
                    server.login(user, password)
                server.send_message(message)
        return True, "sent"
    except (smtplib.SMTPException, OSError) as error:
        logger.warning("email notify failed: %s", error)
        return False, str(error)


def _deliver(subject, body):
    results = {}
    if webhook_configured():
        results["webhook"] = _send_webhook(f"*{subject}*\n{body}")
    if email_configured():
        results["email"] = _send_email(subject, body)
    return results


# ============================================================================
# Public API
# ============================================================================

def notify_alert(alert, device_id="local-server"):
    """Called for every newly-created alert; sends if it meets the threshold."""
    if not enabled():
        return
    if SEVERITY_RANK.get(str(alert.get("severity", "")).lower(), 9) > _min_rank():
        return
    key = (device_id, alert.get("category"), alert.get("description"))
    now = time.time()
    if now - _recent.get(key, 0) < _COOLDOWN_SECONDS:
        return
    _recent[key] = now
    subject = f"[Sentinel] {alert.get('severity', '').upper()} on {device_id}: {alert.get('category')}"
    body = f"{alert.get('description')}\n\nDevice: {device_id}\nTime: {alert.get('timestamp')}"
    threading.Thread(target=_deliver, args=(subject, body), daemon=True).start()


def send_test():
    """Send a test message to every configured channel; returns per-channel result."""
    if not enabled():
        return {"enabled": False, "detail": "No channels configured (set SMTP_* or ALERT_WEBHOOK_URL)."}
    results = _deliver("[Sentinel] Test notification", "This is a test alert from Sentinel. Notifications are working.")
    return {"enabled": True, "results": {k: {"ok": v[0], "detail": v[1]} for k, v in results.items()}}


def send_fleet_summary(devices_with_health):
    if not enabled():
        return False
    lines = ["Sentinel daily fleet summary", ""]
    for d in devices_with_health:
        h = d.get("health", {})
        lines.append(f"- {d.get('nickname')} ({d.get('os_type')}): {d.get('status')}, health {h.get('score', '?')}/100 ({h.get('grade', '?')})")
    if len(lines) == 2:
        lines.append("No devices registered.")
    _deliver("[Sentinel] Daily fleet summary", "\n".join(lines))
    return True

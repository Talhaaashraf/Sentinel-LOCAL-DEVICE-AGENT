"""Copy-paste install one-liners and the self-contained agent bootstrap scripts.

Flow the technician sees in the dashboard:
    1. Generate a one-time token (existing /api/agents/generate-token).
    2. Copy ONE line and run it on the target laptop (same LAN):
         Windows : irm http://SERVER:PORT/install.ps1?t=TOKEN | iex
         Linux/mac: curl -fsSL http://SERVER:PORT/install.sh?t=TOKEN | sh

The scripts download the agent source bundle from THIS server (/agent-bundle.zip),
write agent_config.json from the token, install psutil+requests into a local venv
(or --break-system-packages fallback), and launch the agent. No inbound ports are
opened on the laptop; the agent only makes outbound calls back to this server.
"""

import io
import os
import zipfile
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

router = APIRouter()
AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
BUNDLE_FILES = [
    "monitor_agent.py", "command_channel.py", "remote_tools.py", "tool_schema.py", "tool_catalog.json",
    "tools_common.py", "tools_windows.py", "tools_unix.py", "tools_stress.py",
    "security_checks.py", "performance_checks.py", "event_logs.py", "agent_requirements.txt",
]


def _base_url(request: Request):
    configured = os.getenv("PUBLIC_SERVER_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


@router.get("/agent-bundle.zip")
def agent_bundle():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in BUNDLE_FILES:
            path = AGENT_DIR / name
            if path.exists():
                archive.write(path, name)
    buffer.seek(0)
    return Response(content=buffer.read(), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=agent-bundle.zip"})


@router.get("/install.ps1", response_class=PlainTextResponse)
def install_ps1(request: Request, t: str = "", persist: str = "", nickname: str = ""):
    server = _base_url(request)
    persist_flag = "$true" if persist in ("1", "true", "yes") else "$false"
    script = f"""# Sentinel agent installer (Windows)
$ErrorActionPreference = 'Stop'
$Server = '{server}'
$Token  = '{t}'
$Persist = {persist_flag}
$Nick = '{nickname}'
if (-not $Nick) {{ $Nick = $env:COMPUTERNAME }}
$Dir = Join-Path $env:LOCALAPPDATA 'SentinelAgent'
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
Write-Host "Downloading Sentinel agent from $Server ..."
Invoke-WebRequest -Uri "$Server/agent-bundle.zip" -OutFile "$Dir\\agent.zip"
Expand-Archive -Path "$Dir\\agent.zip" -DestinationPath $Dir -Force
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) {{ $py = (Get-Command py -ErrorAction SilentlyContinue).Source }}
if (-not $py) {{ Write-Error 'Python 3 is required. Install it from https://www.python.org/downloads/ (tick Add to PATH), then re-run.'; return }}
Write-Host 'Installing agent dependencies ...'
& $py -m pip install --quiet --user psutil requests 2>$null
$config = @{{ server_url = $Server; AGENT_TOKEN = $Token; nickname = $Nick; interval_seconds = 10 }} | ConvertTo-Json
Set-Content -Path "$Dir\\agent_config.json" -Value $config -Encoding UTF8
if ($Persist) {{
  $action  = New-ScheduledTaskAction -Execute $py -Argument "$Dir\\monitor_agent.py" -WorkingDirectory $Dir
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  Register-ScheduledTask -TaskName 'SentinelAgent' -Action $action -Trigger $trigger -Force | Out-Null
  Start-ScheduledTask -TaskName 'SentinelAgent'
  Write-Host 'Installed and started as a scheduled task. Remove with: Unregister-ScheduledTask -TaskName SentinelAgent -Confirm:$false'
}} else {{
  Write-Host 'Starting agent. Keep this window open; close it to stop.' -ForegroundColor Green
  & $py "$Dir\\monitor_agent.py"
}}
"""
    return script


@router.get("/install.sh", response_class=PlainTextResponse)
def install_sh(request: Request, t: str = "", persist: str = "", nickname: str = ""):
    server = _base_url(request)
    persist_flag = "1" if persist in ("1", "true", "yes") else "0"
    script = f"""#!/usr/bin/env sh
# Sentinel agent installer (Linux / macOS)
set -eu
SERVER='{server}'
TOKEN='{t}'
PERSIST='{persist_flag}'
NICK='{nickname}'
[ -z "$NICK" ] && NICK="$(hostname)"
DIR="$HOME/.local/share/sentinel-agent"
mkdir -p "$DIR"
echo "Downloading Sentinel agent from $SERVER ..."
curl -fsSL "$SERVER/agent-bundle.zip" -o "$DIR/agent.zip"
( cd "$DIR" && (unzip -o -q agent.zip || python3 -c "import zipfile;zipfile.ZipFile('agent.zip').extractall('.')") )
PY="$(command -v python3 || true)"
[ -z "$PY" ] && {{ echo "Python 3 is required (install python3)"; exit 1; }}
echo "Installing agent dependencies ..."
"$PY" -m venv "$DIR/venv" 2>/dev/null && PY="$DIR/venv/bin/python" || true
"$PY" -m pip install --quiet psutil requests 2>/dev/null || "$PY" -m pip install --quiet --break-system-packages psutil requests 2>/dev/null || true
cat > "$DIR/agent_config.json" <<EOF
{{"server_url":"$SERVER","AGENT_TOKEN":"$TOKEN","nickname":"$NICK","interval_seconds":10}}
EOF
if [ "$PERSIST" = "1" ] && command -v systemctl >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/sentinel-agent.service" <<EOF
[Unit]
Description=Sentinel agent
[Service]
ExecStart=$PY $DIR/monitor_agent.py
WorkingDirectory=$DIR
Restart=always
[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now sentinel-agent.service
  echo "Installed as a user service. Stop with: systemctl --user disable --now sentinel-agent"
else
  echo "Starting agent. Press Ctrl+C to stop."
  exec "$PY" "$DIR/monitor_agent.py"
fi
"""
    return script

# Sentinel — AI-Powered Device Health Monitor

A local, Wazuh-inspired, AI-assisted device health and security dashboard for Windows, macOS, and Linux. FastAPI and `psutil` continuously collect telemetry, a local rules engine persists alerts in SQLite, and an optional Groq technician explains issues — including OS event log entries — in plain language.

## Windows / VS Code setup

Open the `Project-tool1` folder in VS Code and run these commands in its terminal:

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

To enable the AI technician, create a Groq account at `https://console.groq.com/keys`, create an API key, and put it in `.env`:

```text
GROQ_API_KEY=your_actual_key
GROQ_MODEL=openai/gpt-oss-20b
ADMIN_API_KEY=replace_with_a_long_random_admin_key
DASHBOARD_PASSWORD=replace_with_a_separate_dashboard_password
```

## Local network security

This server is designed for local-network use only. It does not provide built-in HTTPS/TLS. Do not expose it directly to the public internet. If remote access is required, put a reverse proxy such as nginx or Caddy with HTTPS, authentication, and appropriate firewall rules in front of it.

Uvicorn should bind to `127.0.0.1` by default:

```powershell
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Only bind to `0.0.0.0` or a LAN address when you understand the exposure and have network controls in place. Set `ADMIN_API_KEY` in `.env`; the Agents provisioning endpoint requires it in the `X-Admin-Key` header.

**Dashboard login.** Set `DASHBOARD_PASSWORD` (or, as a fallback, `ADMIN_API_KEY`) in `.env` and the entire dashboard — pages, REST API, and the `/ws/monitor` WebSocket — requires signing in at `/login` first, via a signed session cookie. Leave both unset and the dashboard stays open with no login, exactly as it did before this option existed. This is a single shared password for the one operator of this tool, not a multi-user system. Sign out any time from the sidebar or at `/logout`.

Keep `.env` private. `GROQ_MODEL` is optional and defaults to `openai/gpt-oss-20b`; set it when you need to switch models. The monitor, rules engine, WebSocket, and alert history work without `GROQ_API_KEY`; only AI diagnosis and chat are disabled.

## Run

```powershell
uvicorn backend.main:app --reload
```

Open `http://127.0.0.1:8000` in a browser. The dashboard opens a WebSocket at `/ws/monitor` and refreshes live snapshots every five seconds. The REST endpoints include:

- `GET /api/diagnostics`
- `GET /api/diagnostics/health`
- `GET /api/diagnostics/security` — read-only firewall/antivirus/disk-encryption/patch/listening-port checks, cached 5 minutes and refreshed automatically in the background
- `GET` and `POST /api/diagnostics/performance` — on-demand CPU/disk/network benchmark (`GET` returns the last cached result or 404, `POST` runs a fresh test); this briefly writes a temp file and uses CPU/network, so it never runs on a timer for the local server
- `GET /api/diagnostics/logs` — recent Warning/Error/Critical entries from the OS event log (Windows Event Viewer, journald, or the macOS unified log), cached 10 minutes and refreshed automatically in the background
- `POST /api/diagnostics/logs/diagnose` — AI root-cause analysis of the latest event log snapshot
- `GET /api/alerts`
- `POST /api/agent/diagnose`
- `GET` and `POST /api/agent/chat`
- `POST /api/agent/action`

Every registered remote device also exposes `GET /api/agents/{device_id}/security`, `GET /api/agents/{device_id}/performance`, and `GET /api/agents/{device_id}/logs` once it has reported that data.

## Add remote agents

Open the **Agents** tab and choose **Add new device**. Click **Generate agent token**, then copy the token into the remote machine's `agent/agent_config.json`:

```json
{
	"server_url": "http://central-server:8000",
	"AGENT_TOKEN": "token-from-the-dashboard",
	"nickname": "Finance laptop",
	"interval_seconds": 10
}
```

The agent can run from source on Windows, macOS, or Linux:

```powershell
pip install -r agent_requirements.txt
python monitor_agent.py
```

Use the platform installer helpers in `agent/` for background startup. For binaries, build on the target operating system with PyInstaller and place them under `agent_builds/`; PyInstaller does not cross-compile. The central server marks devices offline after 30 seconds without a report and keeps device-scoped snapshots and alerts in SQLite.

Every report from an agent also includes a read-only security snapshot (refreshed every 5 minutes), a read-only event log snapshot (refreshed every 10 minutes), and, unless disabled, a periodic performance benchmark (`enable_performance_benchmark` / `performance_interval_seconds` in `agent_config.json`, default every 30 minutes) — see `agent/README.md`.

All local actions are explicitly whitelisted and read-only. No command execution, file deletion, or process termination is performed. The performance benchmark is the one exception: it writes a small temp file to measure real disk throughput and deletes it immediately afterward. The earlier `device_diagnostic.py` command-line tool remains available if needed.

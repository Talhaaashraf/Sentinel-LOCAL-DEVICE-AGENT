# Running Sentinel with Docker + nginx

This brings up three containers: **nginx** (the only published port, 80) in front of
the **backend** (FastAPI app), with **ollama** as the local LLM brain.

```
browser / agents ──:80──► nginx ──► backend:8000 ──► ollama:11434
                                       │
                                  sentinel-data volume (SQLite + saved cases)
```

## 1. Configure

```bash
cd sentinel-local-device-agent
cp .env.docker.example .env
# edit .env: set DASHBOARD_PASSWORD and ADMIN_API_KEY (and ideally PUBLIC_SERVER_URL)
```

Set `PUBLIC_SERVER_URL` to this machine's LAN address (e.g. `http://192.168.1.10`) so the
agent install commands always point back at the right host.

## 2. Start

```bash
docker compose up -d --build
```

Then pull the model once (the brain is free and local; ~2 GB):

```bash
docker compose exec ollama ollama pull qwen2.5:3b-instruct
```

Open **http://<this-host>/** and log in with `DASHBOARD_PASSWORD`.
Without the model, the tool still works on its rule-based engine.

## 3. Add a laptop

Dashboard → **Agents → Add new device → Generate agent token**, then run the shown
one-liner on the target laptop (now on port 80, no `:8000`):

- Windows: `irm "http://<host>/install.ps1?t=TOKEN" | iex`
- Linux/macOS: `curl -fsSL "http://<host>/install.sh?t=TOKEN" | sh`

## Manage

```bash
docker compose ps            # status
docker compose logs -f backend
docker compose restart backend
docker compose down          # stop (keeps volumes/data)
docker compose down -v       # stop and DELETE all data (DB, cases, models)
```

## Data & persistence

- `sentinel-data` — the SQLite database (devices, alerts, commands, schedules).
- `decisions-cases` — saved diagnosis cases (Markdown).
- `ollama-data` — downloaded models.

All survive `up`/`down`/`restart`; only `down -v` removes them.

## HTTPS (optional)

This stack serves plain HTTP for a trusted LAN. To add TLS, put a cert in
`deploy/certs/`, uncomment the `443` block in `deploy/nginx.conf`, uncomment the
`443:443` and certs mount in `docker-compose.yml`, and restart nginx.

## Notes

- Raw remote shell stays disabled unless you set `ALLOW_REMOTE_SHELL=true` in `.env`.
- The backend runs with `--proxy-headers`, so it sees the real client host/scheme
  through nginx (correct install URLs, WebSocket upgrade, large uploads all handled).

# Sentinel backend image — the FastAPI app that serves the dashboard, API and
# WebSocket, and builds the agent install bundle on demand. Runs behind nginx.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code. agent/ must be present: install_api.agent_bundle() zips it at runtime
# so target laptops can download the agent from this server.
COPY backend/ ./backend/
COPY agent/ ./agent/
COPY frontend/ ./frontend/
COPY decisions/ ./decisions/

# Persistent data lives here (SQLite); mounted as a volume by compose.
RUN mkdir -p /app/data /app/decisions/cases

EXPOSE 8000

# --proxy-headers + trusting the forwarded IPs make request.base_url reflect the
# real host/scheme seen by nginx, so generated install one-liners are correct.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]

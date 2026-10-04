"""Outbound command channel for the agent.

The agent polls GET /api/agents/commands/next for approved tool commands, runs
each one through remote_tools.run_tool, streams progress to
POST /api/agents/commands/{id}/progress, and posts the final result to
POST /api/agents/commands/{id}/result. All traffic is agent-initiated and
authenticated with the agent's bearer token, so the laptop needs no inbound
ports or firewall changes. Each command runs in its own thread so a long repair
or stress test never blocks polling or cancellation.
"""

import logging
import threading
import time

import requests

from remote_tools import ToolContext, run_tool
from tool_schema import ToolValidationError

POLL_INTERVAL_IDLE = 3
POLL_INTERVAL_BUSY = 1
REQUEST_TIMEOUT = 20


class CommandChannel:
    def __init__(self, server_url, agent_token, device_id, backup_dir, allow_shell=False):
        self.base = server_url.rstrip("/")
        self.token = agent_token
        self.device_id = device_id
        self.backup_dir = backup_dir
        self.allow_shell = allow_shell
        self.headers = {"Authorization": f"Bearer {agent_token}"}
        self._cancelled = set()
        self._running = {}
        self._lock = threading.Lock()

    # -- server I/O -------------------------------------------------------
    def _post(self, path, json):
        try:
            requests.post(f"{self.base}{path}", json=json, headers=self.headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as error:
            logging.warning("command post to %s failed: %s", path, error)

    def _emit_progress(self, payload):
        run_id = payload.get("run_id")
        self._post(f"/api/agents/commands/{run_id}/progress", payload)

    def _is_cancelled(self, run_id):
        with self._lock:
            return run_id in self._cancelled

    # -- command execution ------------------------------------------------
    def _execute(self, command):
        run_id = command["id"]
        tool_name = command["tool"]
        args = command.get("args") or {}
        ctx = ToolContext(run_id, self.backup_dir, emit=self._emit_progress, is_cancelled=self._is_cancelled)
        started = time.time()
        try:
            result = run_tool(tool_name, args, ctx=ctx, allow_shell=self.allow_shell,
                              server_url=self.base, agent_token=self.token)
            payload = {"run_id": run_id, "status": "done", "result": result, "duration_seconds": round(time.time() - started, 1)}
        except ToolValidationError as error:
            payload = {"run_id": run_id, "status": "rejected", "error": str(error)}
        except Exception as error:  # a tool failing must not take the channel down
            logging.exception("tool %s failed", tool_name)
            payload = {"run_id": run_id, "status": "error", "error": f"{type(error).__name__}: {error}"}
        self._post(f"/api/agents/commands/{run_id}/result", payload)
        with self._lock:
            self._running.pop(run_id, None)
            self._cancelled.discard(run_id)

    def _handle(self, command):
        if command.get("action") == "cancel":
            with self._lock:
                self._cancelled.add(command["id"])
            return
        thread = threading.Thread(target=self._execute, args=(command,), daemon=True)
        with self._lock:
            self._running[command["id"]] = thread
        thread.start()

    # -- main loop --------------------------------------------------------
    def poll_once(self):
        try:
            response = requests.get(f"{self.base}/api/agents/commands/next", headers=self.headers, timeout=REQUEST_TIMEOUT)
            if response.status_code != 200:
                return 0
            commands = response.json().get("commands", [])
        except (requests.RequestException, ValueError):
            return 0
        for command in commands:
            self._handle(command)
        return len(commands)

    def run_forever(self):
        logging.info("Command channel started for device %s", self.device_id)
        while True:
            count = self.poll_once()
            with self._lock:
                busy = bool(self._running)
            time.sleep(POLL_INTERVAL_BUSY if (count or busy) else POLL_INTERVAL_IDLE)

    def start(self):
        thread = threading.Thread(target=self.run_forever, daemon=True)
        thread.start()
        return thread

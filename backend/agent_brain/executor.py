"""Run a toolkit tool on the right machine: in-process for this server, via the task queue for agents."""

import asyncio
import os
import platform
import time
from dataclasses import dataclass

from agent.toolkit import get_tool, run_tool

from ..device_store import get_device
from ..task_queue import CLAIM_TIMEOUT_SECONDS, enqueue_task, expire_stale_tasks, get_task

LOCAL_DEVICE_ID = "local"


@dataclass(frozen=True)
class Target:
    device_id: str
    os_type: str
    label: str
    status: str = "online"

    @property
    def is_local(self):
        return self.device_id == LOCAL_DEVICE_ID


def resolve_target(device_id):
    if not device_id or device_id in (LOCAL_DEVICE_ID, "local-server"):
        label = "Sentinel server container" if os.getenv("SENTINEL_IN_DOCKER") else f"This server ({platform.node()})"
        return Target(LOCAL_DEVICE_ID, platform.system(), label)
    device = get_device(device_id)
    if not device:
        return None
    return Target(device["device_id"], device["os_type"], device["nickname"] or device["hostname"], device["status"])


async def execute(target, tool_id, args, max_risk, session_id=None):
    if target.is_local:
        return await asyncio.to_thread(run_tool, tool_id, args, max_risk, target.os_type)

    item = get_tool(tool_id)
    timeout = item.timeout if item else 90
    task_id = enqueue_task(target.device_id, tool_id, args, max_risk, timeout, session_id)
    deadline = time.monotonic() + CLAIM_TIMEOUT_SECONDS + timeout + 70
    while time.monotonic() < deadline:
        await asyncio.sleep(1)
        task = get_task(task_id)
        if task and task["status"] in ("done", "failed", "expired"):
            result = task["result"] or {}
            return {"tool_id": tool_id, "args": args, **result, "remote_task_id": task_id}
        if task and task["status"] == "queued":
            await asyncio.to_thread(expire_stale_tasks)
    return {"tool_id": tool_id, "args": args, "ok": False, "summary": f"No result from {target.label} in time", "data": None, "remote_task_id": task_id}

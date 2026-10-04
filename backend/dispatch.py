"""Bridge the agent_loop's synchronous tool calls onto the async command queue.

agent_loop runs in a worker thread and calls dispatch_tool(...) expecting the
result. Here we create a queued command (read tools skip approval), then poll
command_store until the agent posts a result or we time out. Change/stress tools
are never dispatched this way — they go through the approval endpoints.
"""

import time

from . import command_store

POLL = 0.5


def dispatch_read_tool(device_id, tool_name, kind, args, session_id, timeout=180, requested_by="ai"):
    """Create a read command and block until the agent returns a result."""
    command = command_store.create_command(
        device_id=device_id, tool=tool_name, kind=kind, args=args,
        requested_by=requested_by, needs_approval=False, reason="AI investigation", session_id=session_id,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = command_store.get_command(command["id"])
        if current["status"] in command_store.FINAL_STATES:
            return {"status": "done" if current["status"] == "done" else "error",
                    "result": current.get("result"), "error": current.get("error"),
                    "command_id": command["id"]}
        time.sleep(POLL)
    return {"status": "error", "error": f"Agent did not respond within {timeout}s (is it online?)", "command_id": command["id"]}

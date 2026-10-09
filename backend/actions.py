"""Legacy read-only action ids, now served by the shared toolkit registry."""

from agent.toolkit import run_tool

LEGACY_ACTIONS = {
    "list_top_processes": "top_processes",
    "check_disk_cleanup_candidates": "cleanup_candidates",
    "check_startup_programs": "startup_programs",
    "check_network_details": "check_internet",
}


def run_action(action_id):
    tool_id = LEGACY_ACTIONS.get(action_id, action_id)
    result = run_tool(tool_id, {}, max_risk="read")
    if result["summary"].startswith("Unknown or disallowed tool") or result["summary"].endswith("(limit: read)"):
        raise ValueError("Unknown or disallowed action")
    return {"action_id": action_id, "result": {**result, "read_only": True}}

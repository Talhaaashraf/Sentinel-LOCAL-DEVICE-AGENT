"""Server-side authority on which tools exist and which need approval.

The server reads its own copy of agent/tool_catalog.json — never a list sent by
an agent — so an agent cannot relabel a change tool as read-only. Raw shell is
refused outright unless ALLOW_REMOTE_SHELL=true is set in .env.
"""

import importlib.util
import os
from pathlib import Path

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "agent" / "tool_schema.py"
_spec = importlib.util.spec_from_file_location("sentinel_tool_schema", _SCHEMA_PATH)
tool_schema = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool_schema)

ToolValidationError = tool_schema.ToolValidationError
CATALOG = tool_schema.load_catalog()
TOOLS = tool_schema.tools_by_name(CATALOG)

OS_TO_PLATFORM = {"windows": "windows", "linux": "linux", "darwin": "darwin", "macos": "darwin"}


def shell_allowed():
    return os.getenv("ALLOW_REMOTE_SHELL", "").strip().lower() in ("1", "true", "yes")


def platform_for(os_type):
    return OS_TO_PLATFORM.get(str(os_type or "").lower(), "linux")


def catalog_for(os_type=None):
    platform_key = platform_for(os_type) if os_type else None
    tools = []
    for tool in CATALOG["tools"]:
        if tool["kind"] == "shell" and not shell_allowed():
            continue
        if platform_key and platform_key not in tool["platforms"]:
            continue
        tools.append(tool)
    return tools


def check(tool_name, args, os_type=None):
    """Validate a request. Returns (tool, clean_args, needs_approval) or raises ToolValidationError."""
    tool = TOOLS.get(tool_name)
    if not tool:
        raise ToolValidationError(f"Unknown tool: {tool_name}")
    if tool["kind"] == "shell" and not shell_allowed():
        raise ToolValidationError("Remote shell is disabled. Set ALLOW_REMOTE_SHELL=true in .env to enable it.")
    if os_type and platform_for(os_type) not in tool["platforms"]:
        raise ToolValidationError(f"{tool_name} is not available on {os_type}")
    clean = tool_schema.validate_args(tool, args or {})
    return tool, clean, tool_schema.needs_approval(tool)


def llm_tool_specs(os_type=None, kinds=("read", "change", "stress")):
    """Catalog entries in the OpenAI/Ollama function-calling format."""
    specs = []
    for tool in catalog_for(os_type):
        if tool["kind"] not in kinds:
            continue
        properties, required = {}, []
        for name, spec in tool.get("params", {}).items():
            prop = {"type": spec.get("type", "string")}
            if spec.get("enum"):
                prop["enum"] = spec["enum"]
            if spec.get("items"):
                prop["items"] = spec["items"]
            if spec.get("description"):
                prop["description"] = spec["description"]
            properties[name] = prop
            if spec.get("required"):
                required.append(name)
        approval = " (NEEDS TECHNICIAN APPROVAL — proposing it does not run it)" if tool["kind"] != "read" else ""
        specs.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"] + approval,
                "parameters": {"type": "object", "properties": properties, "required": required},
            },
        })
    return specs

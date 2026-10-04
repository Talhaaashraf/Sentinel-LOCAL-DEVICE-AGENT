"""Tool catalog loading and argument validation, shared by the agent and the server.

The catalog (tool_catalog.json) is the single list of everything the server may
ask an agent to do. The server reads its own copy to decide what needs approval;
the agent reads its copy to refuse anything not listed. Both validate arguments
with validate_args() so a malformed request never reaches a tool.
"""

import json
import platform
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    _BASE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    _BASE = Path(__file__).resolve().parent
CATALOG_PATH = _BASE / "tool_catalog.json"

APPROVAL_KINDS = {"change", "stress", "shell"}


class ToolValidationError(ValueError):
    pass


def load_catalog(path=CATALOG_PATH):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def tools_by_name(catalog=None):
    catalog = catalog or load_catalog()
    return {tool["name"]: tool for tool in catalog["tools"]}


def current_platform():
    """windows, linux or darwin — the keys used in each tool's platforms list."""
    return platform.system().lower()


def _coerce(name, spec, value):
    kind = spec.get("type", "string")
    if kind == "integer":
        if isinstance(value, bool):
            raise ToolValidationError(f"{name} must be an integer")
        try:
            value = int(value)
        except (TypeError, ValueError) as error:
            raise ToolValidationError(f"{name} must be an integer") from error
        if "minimum" in spec and value < spec["minimum"] and not (value == spec.get("default") == 0):
            raise ToolValidationError(f"{name} must be >= {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            raise ToolValidationError(f"{name} must be <= {spec['maximum']}")
        return value
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "1", "yes"):
            return True
        if str(value).lower() in ("false", "0", "no", ""):
            return False
        raise ToolValidationError(f"{name} must be true or false")
    if kind == "array":
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, list):
            raise ToolValidationError(f"{name} must be a list")
        allowed = (spec.get("items") or {}).get("enum")
        cleaned = []
        for item in value:
            item = str(item)
            if allowed and item not in allowed:
                raise ToolValidationError(f"{name} item {item!r} is not one of {allowed}")
            cleaned.append(item)
        return cleaned
    value = "" if value is None else str(value)
    if len(value) > 4000:
        raise ToolValidationError(f"{name} is too long")
    if spec.get("enum") and value not in spec["enum"]:
        raise ToolValidationError(f"{name} must be one of {spec['enum']}")
    return value


def validate_args(tool, args):
    """Return a clean args dict with defaults filled in, or raise ToolValidationError."""
    args = dict(args or {})
    params = tool.get("params", {})
    unknown = set(args) - set(params)
    if unknown:
        raise ToolValidationError(f"Unknown argument(s) for {tool['name']}: {', '.join(sorted(unknown))}")
    clean = {}
    for name, spec in params.items():
        if name in args and args[name] is not None:
            clean[name] = _coerce(name, spec, args[name])
        elif spec.get("required"):
            raise ToolValidationError(f"{tool['name']} requires {name}")
        else:
            clean[name] = spec.get("default")
        if spec.get("required") and clean[name] in ("", None, []):
            raise ToolValidationError(f"{tool['name']} requires {name}")
    return clean


def needs_approval(tool):
    return tool.get("kind") in APPROVAL_KINDS

#!/usr/bin/env bash
set -euo pipefail
# MUST run on an actual Mac. PyInstaller does not cross-compile.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 -m pip install pyinstaller
mkdir -p "$ROOT/agent_builds/macos" "$ROOT/agent_build_work"
python3 -m PyInstaller --clean --onefile --name DeviceHealthAgent --console --distpath "$ROOT/agent_builds/macos" --workpath "$ROOT/agent_build_work" --specpath "$ROOT/agent_build_work" "$ROOT/agent/monitor_agent.py"
chmod +x "$ROOT/agent_builds/macos/DeviceHealthAgent"
cp "$ROOT/agent/agent_config.example.json" "$ROOT/agent_builds/macos/agent_config.example.json"
echo "SUCCESS: macOS binary created at $ROOT/agent_builds/macos/DeviceHealthAgent"

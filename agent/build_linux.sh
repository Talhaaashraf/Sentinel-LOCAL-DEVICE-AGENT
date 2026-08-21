#!/usr/bin/env bash
set -euo pipefail
# MUST run on actual Linux. PyInstaller does not cross-compile.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 -m pip install pyinstaller
mkdir -p "$ROOT/agent_builds/linux" "$ROOT/agent_build_work"
python3 -m PyInstaller --clean --onefile --name DeviceHealthAgent --console --distpath "$ROOT/agent_builds/linux" --workpath "$ROOT/agent_build_work" --specpath "$ROOT/agent_build_work" "$ROOT/agent/monitor_agent.py"
chmod +x "$ROOT/agent_builds/linux/DeviceHealthAgent"
cp "$ROOT/agent/agent_config.example.json" "$ROOT/agent_builds/linux/agent_config.example.json"
echo "SUCCESS: Linux binary created at $ROOT/agent_builds/linux/DeviceHealthAgent"

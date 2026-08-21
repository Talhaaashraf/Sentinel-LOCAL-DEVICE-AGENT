# Sentinel Monitor Agent

The standalone `monitor_agent.py` works on Windows, macOS, and Linux. It reads `agent_config.json`, registers once with the central server, then sends read-only health snapshots at the configured interval.

Example configuration (`agent_config.json`):

```json
{
  "server_url": "http://127.0.0.1:8000",
  "token": "paste-token-from-admin",
  "nickname": "Office laptop",
  "interval_seconds": 10
}
```

Install the fallback on a machine without a prebuilt binary:

```bash
pip install -r agent_requirements.txt
python monitor_agent.py
```

## Building Installers

Build on the target operating system only. PyInstaller does not cross-compile.

- Windows: run `powershell -ExecutionPolicy Bypass -File agent/build_windows.ps1`; output is `agent_builds/windows/DeviceHealthAgent.exe`.
- macOS: run `chmod +x agent/build_macos.sh && ./agent/build_macos.sh` on an actual Mac; output is `agent_builds/macos/DeviceHealthAgent`.
- Linux: run `chmod +x agent/build_linux.sh && ./agent/build_linux.sh` on actual Linux; output is `agent_builds/linux/DeviceHealthAgent`.

Each build also copies `agent_config.example.json` beside the binary. Copy it to `agent_config.json`, replace the server URL and generated token, and keep it next to the executable before starting the agent.

Build manually on the target OS:

```bash
pip install pyinstaller
python build_installer.py
```

The manual Python path remains available on every platform.

`install_windows.ps1`, `install_linux.sh`, and `install_mac.sh` install the binary/configuration and register an idempotent background startup task. The agent retries unreachable servers with exponential backoff and writes diagnostics to `agent_log.txt`.

"""Build the standalone agent on the target operating system."""

import platform
import subprocess

name = "monitor_agent.exe" if platform.system() == "Windows" else "monitor_agent"
subprocess.run(["pyinstaller", "--onefile", "--name", name, "monitor_agent.py"], check=True)
print(f"Built dist/{name}. Build on each target OS; PyInstaller does not cross-compile.")

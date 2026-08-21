$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot 'venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = (Get-Command python).Source }
& $python -m pip install -r (Join-Path $PSScriptRoot 'agent_requirements.txt')
$outputDir = Join-Path $projectRoot 'agent_builds\windows'
$workDir = Join-Path $projectRoot 'agent_build_work'
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
$source = Join-Path $PSScriptRoot 'monitor_agent.py'
& $python -m PyInstaller --clean --onefile --name DeviceHealthAgent --console --distpath $outputDir --workpath $workDir --specpath $workDir $source
# Use --noconsole later for a silent background agent if desired.
Copy-Item (Join-Path $PSScriptRoot 'agent_config.example.json') (Join-Path $outputDir 'agent_config.example.json') -Force
$exe = Join-Path $outputDir 'DeviceHealthAgent.exe'
if (-not (Test-Path $exe) -or (Get-Item $exe).Length -le 0) { throw "Build failed: $exe was not created." }
Write-Host "SUCCESS: Windows installer created at $exe"
Write-Host "Copy agent_config.example.json to agent_config.json, fill in the real token, and place it beside the exe."

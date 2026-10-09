# Sentinel agent installer for Windows.
# Run in an elevated (Administrator) PowerShell:
#   irm "__SERVER_URL__/install/windows.ps1?token=<token>" | iex
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Server = '__SERVER_URL__'
$Token = '__TOKEN__'
$InstallDir = Join-Path $env:ProgramData 'SentinelAgent'
$TaskName = 'Sentinel Agent'

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'Please run this command from an elevated PowerShell window (Run as Administrator).'
}

Write-Host "[Sentinel] Installing agent to $InstallDir (server: $Server)"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*SentinelAgent*" -and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# 1. Prefer a prebuilt binary if the server has one; otherwise run from Python source.
$exe = Join-Path $InstallDir 'SentinelAgent.exe'
$mode = 'python'
try {
  Invoke-WebRequest -UseBasicParsing -Uri "$Server/api/agents/binary/windows?token=$Token" -OutFile $exe
  $mode = 'binary'
  $AgentDir = $InstallDir
  $Execute = $exe
  $Arguments = ''
} catch {
  Remove-Item $exe -Force -ErrorAction SilentlyContinue
}

if ($mode -eq 'python') {
  function Find-Python {
    foreach ($candidate in @('py', 'python', 'python3')) {
      $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
      if (-not $cmd -or $cmd.Source -like '*WindowsApps*') { continue }
      $args3 = if ($candidate -eq 'py') { @('-3', '-c', 'import sys; print(sys.executable)') } else { @('-c', 'import sys; print(sys.executable)') }
      try { $path = (& $cmd.Source @args3 2>$null | Select-Object -First 1) } catch { $path = $null }
      if ($path -and (Test-Path $path)) { return $path.Trim() }
    }
    foreach ($path in @("$env:ProgramFiles\Python313\python.exe", "$env:ProgramFiles\Python312\python.exe", "$env:ProgramFiles\Python311\python.exe")) {
      if (Test-Path $path) { return $path }
    }
    return $null
  }
  $python = Find-Python
  if (-not $python) {
    Write-Host '[Sentinel] Python 3 not found; installing it with winget...'
    winget install -e --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $python = Find-Python
    if (-not $python) { throw 'Python 3 is required. Install it from https://www.python.org/downloads/ and re-run this command.' }
  }
  Write-Host "[Sentinel] Using Python at $python"

  $AgentDir = Join-Path $InstallDir 'app'
  $zip = Join-Path $env:TEMP 'sentinel-agent.zip'
  Invoke-WebRequest -UseBasicParsing -Uri "$Server/api/agents/bundle?token=$Token" -OutFile $zip
  New-Item -ItemType Directory -Force -Path $AgentDir | Out-Null
  Expand-Archive -Path $zip -DestinationPath $AgentDir -Force
  Remove-Item $zip -Force

  $venv = Join-Path $InstallDir 'venv'
  if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) { & $python -m venv $venv }
  $venvPython = Join-Path $venv 'Scripts\python.exe'
  & $venvPython -m pip install --quiet --disable-pip-version-check --upgrade psutil requests
  if ($LASTEXITCODE -ne 0) { throw 'pip install failed (check internet access on this device).' }
  $Execute = $venvPython
  $Arguments = '"' + (Join-Path $AgentDir 'monitor_agent.py') + '"'
}

# 2. Configuration (keeps the existing device identity when re-installing with the same token).
$configPath = Join-Path $AgentDir 'agent_config.json'
$keep = $false
if (Test-Path $configPath) {
  try { $existing = Get-Content $configPath -Raw | ConvertFrom-Json; $keep = ($existing.AGENT_TOKEN -eq $Token) } catch { $keep = $false }
}
if (-not $keep) {
  $config = [ordered]@{ server_url = $Server; AGENT_TOKEN = $Token; nickname = $env:COMPUTERNAME; interval_seconds = 10; allow_remediation = $true; max_risk = 'high' }
  [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
}

# 3. Run at startup as SYSTEM so diagnostics and fixes have the rights they need.
$action = if ($Arguments) { New-ScheduledTaskAction -Execute $Execute -Argument $Arguments -WorkingDirectory $AgentDir } else { New-ScheduledTaskAction -Execute $Execute -WorkingDirectory $AgentDir }
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Sentinel AI troubleshooting agent' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "[Sentinel] Agent installed ($mode mode) and running. It will appear in the dashboard within a few seconds."
Write-Host "[Sentinel] Log file: $(Join-Path $AgentDir 'agent_log.txt')"
Write-Host "[Sentinel] Uninstall: irm '$Server/install/uninstall-windows.ps1' | iex"

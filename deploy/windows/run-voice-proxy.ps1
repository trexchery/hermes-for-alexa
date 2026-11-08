# Restart loop for deploy/voice_proxy.py (HA -> :8642 -> voice Hermes on 127.0.0.1:8643). Started hidden by the
# Hermes-Voice-Proxy logon task.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
$logDir = Join-Path $env:LOCALAPPDATA 'hermes-alexa'
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir 'voice-proxy.log'
Set-Location $root
while ($true) {
    if ((Test-Path $log) -and (Get-Item $log).Length -gt 5MB) { Move-Item $log "$log.1" -Force }
    if ($py) { & $py -m deploy.voice_proxy *>> $log } else { 'python not found on PATH' | Add-Content $log }
    "$(Get-Date -Format s) voice proxy exited ($LASTEXITCODE), restarting in 15 s" | Add-Content $log
    Start-Sleep -Seconds 15
}

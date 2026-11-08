# One orchestrator, safe to re-run. Each step checks state first. Stops at the first missing login and prints
# the one command to run; run install.ps1 again afterwards and it continues.
#   powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -Hostname ha-voice.example.com -PrivacyUrl https://example.com/privacy
# The parameters are stored once (state.json); later runs need none. Optional: -TunnelName, -Region, -TimeZone (IANA, e.g. Europe/Berlin).
param([string]$Hostname, [string]$PrivacyUrl, [string]$TunnelName, [string]$Region, [string]$TimeZone)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$N = 13
foreach ($kv in @(@('public_hostname', $Hostname), @('privacy_url', $PrivacyUrl), @('cf_tunnel_name', $TunnelName), @('aws_region', $Region), @('timezone', $TimeZone))) {
    if ($kv[1]) { & python -m deploy.state set "$($kv[0])=$($kv[1])"; if ($LASTEXITCODE -ne 0) { exit 1 } }
}
& python -m deploy.state check public_hostname privacy_url
if ($LASTEXITCODE -ne 0) { exit 2 }
$script:i = 0
function Step([string]$t) { $script:i++; Write-Host "[$($script:i)/$N] $t" -ForegroundColor Cyan }
function NeedLogin([string]$what, [string]$cmd) {
    Write-Host "  MISSING: $what" -ForegroundColor Yellow
    Write-Host "  Run this once, then run install.ps1 again:  $cmd" -ForegroundColor Yellow
    exit 2
}
# Probe a native command: Windows PowerShell 5.1 turns redirected stderr into a terminating error under 'Stop'.
# Leaves $LASTEXITCODE at 1 when the command is missing, 0 or the real code otherwise.
function Quiet([scriptblock]$s) { $e = $ErrorActionPreference; $ErrorActionPreference = 'Continue'; $global:LASTEXITCODE = 1; & $s 2>&1 | Out-Null; $ErrorActionPreference = $e }
function Py([string[]]$a) { & python @a; if ($LASTEXITCODE -ne 0) { Write-Host "  step failed (exit $LASTEXITCODE)" -ForegroundColor Red; exit 1 } }

Step 'Python dependencies (boto3, websockets)'
Quiet { python -c "import boto3, websockets" }
if ($LASTEXITCODE -ne 0) { Py @('-m', 'pip', 'install', '--quiet', 'boto3[crt]', 'websockets') } else { Write-Host '  present' }

Step 'Logins: Docker, cloudflared, AWS, ask CLI'
Quiet { docker info }
if ($LASTEXITCODE -ne 0) { Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'; Write-Host '  starting Docker Desktop ...'; for ($k = 0; $k -lt 36; $k++) { Start-Sleep 5; Quiet { docker info }; if ($LASTEXITCODE -eq 0) { break } } }
if ($LASTEXITCODE -ne 0) { NeedLogin 'Docker engine not running' 'start Docker Desktop and sign in if asked' }
if (-not (Test-Path "$env:USERPROFILE\.cloudflared\cert.pem")) { NeedLogin 'Cloudflare login' 'cloudflared tunnel login' }
$aws = (Get-Command aws -ErrorAction SilentlyContinue).Source; if (-not $aws) { $aws = 'C:\Program Files\Amazon\AWSCLIV2\aws.exe' }
Quiet { & $aws sts get-caller-identity }
if ($LASTEXITCODE -ne 0) { NeedLogin 'AWS credentials' "& '$aws' configure   (or: aws configure sso)" }
Quiet { python -c "import sys; from deploy.skill_deploy import smapi; smapi('get-vendor-list')" }  # finds ask even when npm's bin is not on PATH
if ($LASTEXITCODE -ne 0) { NeedLogin 'ask CLI login (Amazon developer account)' 'ask configure' }
Write-Host '  all logins present'

Step 'Hermes voice profile (profile, keys, config, gateway)'; Py @('-m', 'deploy.hermes_setup')
Step 'Scheduled tasks, Docker autostart, power plan';      & powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install-tasks.ps1
Step 'Start the voice proxy (HA -> :8642 -> voice Hermes)'; Start-ScheduledTask -TaskName 'Hermes-Voice-Proxy'
for ($k = 0; $k -lt 15; $k++) { try { if ((Invoke-WebRequest 'http://127.0.0.1:8642/health' -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { break } } catch {}; Start-Sleep 2 }
Step 'Cloudflare tunnel';                        Py @('-m', 'deploy.tunnel_setup')
Step 'Home Assistant + cloudflared containers';             docker compose -f deploy\ha\compose.yaml up -d; if ($LASTEXITCODE -ne 0) { exit 1 }
Step 'Home Assistant bootstrap (users, tokens, integration)'; Py @('-m', 'deploy.bootstrap_ha')
Step 'Start watchdog now';                                 Start-ScheduledTask -TaskName 'Hermes-Watchdog'
Step 'Cloudflare Access (optional)';                       Py @('-m', 'deploy.cloudflare_access')
Step 'AWS Lambda, pass 1 (role, SSM, function)';           Py @('-m', 'deploy.aws_deploy')
Step 'Alexa skill';                                        Py @('-m', 'deploy.skill_deploy')
Step 'AWS Lambda, pass 2 (trust the skill id)';            Py @('-m', 'deploy.aws_deploy')

Write-Host 'Done. Verify the whole chain with:  python scripts\verify_e2e.py' -ForegroundColor Green

# Runs every 2 minutes. Checks Docker, Home Assistant, the tunnel and Hermes; applies the smallest remedy.
# Logs to %LOCALAPPDATA%\hermes-alexa\watchdog.log (rotated at 1 MB). Never logs secrets.
$ErrorActionPreference = 'Continue'
$dir = Join-Path $env:LOCALAPPDATA 'hermes-alexa'
New-Item -ItemType Directory -Force $dir | Out-Null
$log = Join-Path $dir 'watchdog.log'
$stateFile = Join-Path $dir 'watchdog-state.json'
if ((Test-Path $log) -and (Get-Item $log).Length -gt 1MB) { Move-Item $log "$log.1" -Force }
function Log($m) { "$(Get-Date -Format s) $m" | Add-Content $log }

function Get-Secret([string]$name) {
    $f = Join-Path $dir "secrets\$name.bin"
    if (-not (Test-Path $f)) { return $null }
    Add-Type -AssemblyName System.Security
    $b = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($f), [Text.Encoding]::UTF8.GetBytes('hermes-alexa'), 'CurrentUser')
    [Text.Encoding]::UTF8.GetString($b)
}
function Probe([string]$url, [hashtable]$headers = @{}) {
    try { (Invoke-WebRequest -Uri $url -Headers $headers -TimeoutSec 8 -UseBasicParsing).StatusCode }
    catch { if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 } }
}
$haBad = 0; $hermesBad = 0
if (Test-Path $stateFile) { try { $st = Get-Content $stateFile -Raw | ConvertFrom-Json; $haBad = [int]$st.haBad; $hermesBad = [int]$st.hermesBad } catch {} }

# 1. Docker engine
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Log 'docker down: starting Docker Desktop'
    Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    for ($i = 0; $i -lt 24; $i++) { Start-Sleep 5; docker info *> $null; if ($LASTEXITCODE -eq 0) { break } }
    if ($LASTEXITCODE -ne 0) { Log 'docker still down after 120 s'; exit 1 }
}

# 2. Home Assistant: container health; 3 bad checks in a row -> restart.
$health = (docker inspect -f '{{.State.Health.Status}}' homeassistant 2>$null)
if (-not $health) {
    Log 'container missing: docker compose up -d'
    docker compose -f (Join-Path (Split-Path $PSScriptRoot -Parent) 'ha\compose.yaml') up -d *>> $log
} elseif ($health -ne 'healthy' -or (Probe 'http://127.0.0.1:8123/manifest.json') -notin 200, 403) {  # 403: HA ip ban, a restart does not clear it
    $haBad++
    Log "HA not healthy ($health), bad count $haBad"
    if ($haBad -ge 3 -and $health -ne 'starting') { Log 'restarting homeassistant'; docker restart homeassistant *>> $log; $haBad = 0 }
} else {
    # No authenticated probe here: a rejected token would feed HA's ip_ban and lock out the host. verify_e2e checks tokens.
    $haBad = 0
}

# 3. Tunnel
if ((Probe 'http://127.0.0.1:20251/ready') -ne 200) {
    # cloudflared runs in the hermes-cloudflared container (fixed IP that HA trusts, deploy/ha/compose.yaml).
    if (docker inspect hermes-cloudflared 2>$null) { Log 'tunnel not ready: restarting hermes-cloudflared'; docker restart hermes-cloudflared *>> $log }
    else { Log 'tunnel container missing: docker compose up -d'; docker compose -f (Join-Path (Split-Path $PSScriptRoot -Parent) 'ha\compose.yaml') up -d *>> $log }
} else {
    # 3b. Through Cloudflare: 401/405 from the turn endpoint means edge, Access, tunnel and HA all answered. Log only.
    $h = @{}
    $id = Get-Secret 'cf_access_client_id'; $sec = Get-Secret 'cf_access_client_secret'
    if ($id -and $sec) { $h = @{ 'CF-Access-Client-Id' = $id; 'CF-Access-Client-Secret' = $sec } }
    $host_ = $null
    try { $host_ = (Get-Content (Join-Path $dir 'state.json') -Raw | ConvertFrom-Json).public_hostname } catch {}
    if ($host_) {
        $code = Probe "https://$host_/api/hermes_voice/alexa/v1/turn" $h
        if ($code -notin 401, 405) { Log "public probe returned $code" }
    }
}

# 4. Hermes voice profile gateway (docs/ARCHITECTURE.md), its api_server on 127.0.0.1:8643. `gateway start` spawns it
#    hidden and detached and does nothing when it already runs; a second bad check in a row means hung: restart.
$hh = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA 'hermes' }
$hermes = Join-Path $hh 'bin\hermes.cmd'
$env:HERMES_GATEWAY_INSTALL_START_ON_LOGIN = '0'   # never install a login task or prompt
if ((Probe 'http://127.0.0.1:8643/health') -ne 200) {
    $hermesBad++
    Log "voice gateway /health failed, bad count $hermesBad"
    if ($hermesBad -ge 2) {
        Log 'restarting voice gateway'
        & $hermes -p voice gateway restart *>> $log
        $hermesBad = 0
    } else { & $hermes -p voice gateway start *>> $log }
} else { $hermesBad = 0 }

# 5. Voice proxy (what HA calls on host.docker.internal:8642): its loop task, killed and restarted when it does not answer.
if ((Probe 'http://127.0.0.1:8642/health') -ne 200) {
    Log 'voice proxy not answering: restarting task Hermes-Voice-Proxy'
    Stop-ScheduledTask -TaskName 'Hermes-Voice-Proxy' -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*deploy.voice_proxy*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-ScheduledTask -TaskName 'Hermes-Voice-Proxy'
}
@{ haBad = $haBad; hermesBad = $hermesBad } | ConvertTo-Json | Set-Content $stateFile

# Idempotent: registers the voice-proxy + watchdog logon tasks, Docker Desktop
# AutoStart and the power plan. Runs as the current user (no elevation needed except powercfg on some PCs).
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$me = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited

function New-Settings([TimeSpan]$limit) {
    New-ScheduledTaskSettingsSet -ExecutionTimeLimit $limit -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
}
# conhost --headless: no window, no Windows Terminal tab.
function New-HiddenPs([string]$script) {
    New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`""
}

# 1b. Voice proxy loop (never times out): the only door from HA to the voice Hermes profile (docs/ARCHITECTURE.md).
#     The voice gateway itself is started by the watchdog with `hermes -p voice gateway start`.
$plogon = New-ScheduledTaskTrigger -AtLogOn -User $me
$plogon.Delay = 'PT30S'
Register-ScheduledTask -TaskName 'Hermes-Voice-Proxy' -Force -Principal $principal -Trigger $plogon `
    -Action (New-HiddenPs "$here\run-voice-proxy.ps1") -Settings (New-Settings ([TimeSpan]::Zero)) | Out-Null
Write-Host 'task Hermes-Voice-Proxy registered'

# 2. Watchdog: at logon and every 2 minutes. A 10-minute limit instead of 0, because with IgnoreNew a hung
#    run would otherwise block every later run forever.
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Days 9999)
$wlogon = New-ScheduledTaskTrigger -AtLogOn -User $me
Register-ScheduledTask -TaskName 'Hermes-Watchdog' -Force -Principal $principal -Trigger @($wlogon, $every) `
    -Action (New-HiddenPs "$here\watchdog.ps1") -Settings (New-Settings (New-TimeSpan -Minutes 10)) | Out-Null
Write-Host 'task Hermes-Watchdog registered'

# 3. Docker Desktop starts at logon.
$ds = Join-Path $env:APPDATA 'Docker\settings-store.json'
if (Test-Path $ds) {
    $json = Get-Content $ds -Raw | ConvertFrom-Json
    if ($json.AutoStart -ne $true) {
        Copy-Item $ds "$ds.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
        $json | Add-Member -NotePropertyName AutoStart -NotePropertyValue $true -Force
        [IO.File]::WriteAllText($ds, ($json | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding $false))
        Write-Host 'Docker Desktop AutoStart enabled (applies after Docker Desktop restarts; it may rewrite the file on exit)'
    } else { Write-Host 'Docker Desktop AutoStart already on' }
} else { Write-Host 'Docker settings-store.json not found: skipped' }

# 4. Never sleep or hibernate on AC.
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
Write-Host 'power plan: no sleep on AC'

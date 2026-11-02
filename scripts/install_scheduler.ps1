# Register the league with Windows Task Scheduler: a tick every 15 minutes, and the site watcher
# (deploys the public site as soon as anything changes; restarted within 15 minutes if it stops).
#   powershell -ExecutionPolicy Bypass -File scripts\install_scheduler.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_scheduler.ps1 -Remove
param([switch]$Remove)

$name = "LLM-FF scheduler tick"
$watcherName = "LLM-FF site watcher"
if ($Remove) {
    foreach ($n in $name, $watcherName) {
        Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Removed '$n'."
    }
    return
}

$root = Split-Path -Parent $PSScriptRoot
# A headless console host: no window at all. (powershell -WindowStyle Hidden still flashes one,
# especially with Windows Terminal as the default terminal, which ignores the hidden style.)
function Hidden-Action($script) {
    New-ScheduledTaskAction -Execute "conhost.exe" `
        -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot $script)`"" `
        -WorkingDirectory $root
}

$every15 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName $name -Action (Hidden-Action "tick.ps1") -Trigger $every15 -Settings $settings `
    -Description "AI fantasy football league: syncs data, processes waivers, runs agents (llm-ff)" -Force | Out-Null
Write-Host "Registered '$name' (every 15 minutes, runs as $env:USERNAME while logged on)."

# The watcher runs indefinitely. Its 15-minute trigger only restarts it if it has stopped (a running
# copy makes Task Scheduler ignore the trigger, and a second copy would exit at once anyway).
$watcherTriggers = @((New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(30) -RepetitionInterval (New-TimeSpan -Minutes 15)))
$watcherSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $watcherName -Action (Hidden-Action "watch_site.ps1") -Trigger $watcherTriggers `
    -Settings $watcherSettings -Description "AI fantasy football league: deploys the public site on every change (llm-ff)" `
    -Force | Out-Null
Write-Host "Registered '$watcherName' (starts at logon; restarted within 15 minutes if it stops)."
Write-Host "Logs: logs\scheduler.log   Status: python -m aiffl.scheduler status"

# Register the league scheduler with Windows Task Scheduler: a tick every 15 minutes.
#   powershell -ExecutionPolicy Bypass -File scripts\install_scheduler.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_scheduler.ps1 -Remove
param([switch]$Remove)

$name = "LLM-FF scheduler tick"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "Removed '$name'."
    return
}

$tick = Join-Path $PSScriptRoot "tick.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$tick`"" `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "AI fantasy football league: syncs data, processes waivers, runs agents (llm-ff)" -Force | Out-Null
Write-Host "Registered '$name' (every 15 minutes, runs as $env:USERNAME while logged on)."
Write-Host "Log: logs\scheduler.log   Status: python -m aiffl.scheduler status"

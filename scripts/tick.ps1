# One scheduler tick. Windows Task Scheduler runs this every 15 minutes (see install_scheduler.ps1).
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force logs | Out-Null
& "$root\.venv\Scripts\python.exe" -m aiffl.scheduler tick *>> "$root\logs\scheduler-console.log"

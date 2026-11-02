# The site watcher: deploys the public site as soon as anything changes. Task Scheduler keeps it
# running (see install_scheduler.ps1); a second copy exits at once.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force logs | Out-Null
& "$root\.venv\Scripts\python.exe" -m aiffl.scheduler watch *>> "$root\logs\site-watcher-console.log"

# Registers the "RenesDashboard" scheduled task: the renes dashboard (http://127.0.0.1:8765, and
# http://renes.localhost through Caddy) starts at logon without a console window and restarts if it
# fails. Run once from the repo root: powershell -ExecutionPolicy Bypass -File scripts\install_dashboard_task.ps1
# Remove with: Unregister-ScheduledTask -TaskName RenesDashboard -Confirm:$false
$repo = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path (Split-Path (Get-Command python).Source) "pythonw.exe"
New-Item -ItemType Directory -Force (Join-Path $repo "out") | Out-Null
$log = Join-Path $repo "out\dashboard.log"
$action = New-ScheduledTaskAction -Execute $pythonw -WorkingDirectory $repo `
    -Argument "-m renes dashboard --no-browser --port 8765 --log `"$log`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
Register-ScheduledTask -TaskName RenesDashboard -Description "renes project dashboard (local only)" `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "RenesDashboard registered: $pythonw -m renes dashboard (log: $log)"

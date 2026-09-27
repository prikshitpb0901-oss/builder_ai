# Windows PowerShell Script to Register Bi-Daily 7:00 AM & 7:00 PM Audit Task
# Run in PowerShell as Administrator or current user

$RepoDir = Split-Path -Parent $PSScriptRoot
$Action = New-ScheduledTaskAction -Execute "uv" -Argument "run python scripts/run_daily_audit.py --count 100" -WorkingDirectory $RepoDir
$TriggerAM = New-ScheduledTaskTrigger -Daily -At 7:00AM
$TriggerPM = New-ScheduledTaskTrigger -Daily -At 7:00PM
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName "NorwayAgentDailyAudit" -Action $Action -Trigger @($TriggerAM, $TriggerPM) -Settings $Settings -Description "Automated 7 AM and 7 PM 100-company audit for Norway Intelligence Agent" -Force

Write-Host "✅ Successfully registered 'NorwayAgentDailyAudit' scheduled task for 7:00 AM and 7:00 PM daily!" -ForegroundColor Green
Write-Host "Logs and scorecards will be saved in: $RepoDir\out\scheduled\" -ForegroundColor Cyan

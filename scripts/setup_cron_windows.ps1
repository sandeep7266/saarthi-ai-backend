# Saarthi-AI — Windows Task Scheduler setup (run as Administrator)
param(
    [string]$ApiUrl     = "https://saarthi-ai-api.railway.app",
    [string]$CronSecret = $env:CRON_SECRET
)
if (-not $CronSecret) { Write-Error "Set CRON_SECRET env var"; exit 1 }

$Action  = New-ScheduledTaskAction -Execute "curl.exe" `
           -Argument "-s -X POST `"$ApiUrl/api/v1/cron/run-daily-sync`" -H `"X-Cron-Secret: $CronSecret`""
$Trigger = New-ScheduledTaskTrigger -Daily -At "00:00"
Unregister-ScheduledTask -TaskName "SaarthiAI-DailySync" -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName "SaarthiAI-DailySync" -Action $Action -Trigger $Trigger
Write-Host "Task created — runs daily at midnight." -ForegroundColor Green

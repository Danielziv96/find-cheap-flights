# Creates a Windows Task Scheduler task that runs the flight monitor every 10 minutes
$taskName   = "FlightMonitor_TLV_Greece"
$scriptPath = "c:\projects\find_cheap_flights\run_monitor.bat"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action  = New-ScheduledTaskAction -Execute $scriptPath
$trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 10) -Once -At (Get-Date)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 8) -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action   $action `
    -Trigger  $trigger `
    -Settings $settings `
    -Description "Monitors TLV→Greek Islands flights every 10 min. Alerts when price < $230/person." `
    -RunLevel Highest

Write-Host "Task '$taskName' registered successfully. Running now for first test..."
Start-ScheduledTask -TaskName $taskName

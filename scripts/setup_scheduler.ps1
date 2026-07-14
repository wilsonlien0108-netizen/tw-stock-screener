# 建立 Windows 工作排程：每個平日 20:30 自動執行盤後資料更新
# 以系統管理員身分執行一次即可；移除：schtasks /Delete /TN "TWStockScreenerDaily" /F

$projectDir = Split-Path -Parent $PSScriptRoot
# 優先使用資料夾內的可攜式 Python（pythonw 無視窗），沒有才用系統安裝的 py
$py = Join-Path $projectDir "runtime\pythonw.exe"
if (-not (Test-Path $py)) { $py = (Get-Command py).Source }
$action = New-ScheduledTaskAction -Execute $py `
    -Argument "`"$projectDir\scripts\daily_update.py`"" `
    -WorkingDirectory $projectDir
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 20:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName "TWStockScreenerDaily" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "台股篩選器每日盤後資料更新" -Force

Write-Host "已建立排程工作 TWStockScreenerDaily（平日 20:30）"

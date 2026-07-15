@echo off
chcp 65001 >nul
schtasks /Create /F /TN "TWStockIntradayMonitor" /TR "\"%~dp0runtime\pythonw.exe\" -X utf8 \"%~dp0scripts\intraday_monitor.py\"" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:58
if %errorlevel% equ 0 (
  echo 已設定：每個平日 08:58 自動啟動盤中監控（13:32 自動收工，電腦需開機）。
  echo 取消：schtasks /Delete /TN "TWStockIntradayMonitor" /F
) else (
  echo 設定失敗，請改以系統管理員身分執行本檔案。
)
pause

@echo off
chcp 65001 >nul
schtasks /Create /F /TN "TWStockScreenerDaily" /TR "\"%~dp0runtime\pythonw.exe\" -X utf8 \"%~dp0scripts\daily_update.py\"" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 20:30
if %errorlevel% equ 0 (
  echo.
  echo 已設定：每個平日晚上 8:30 自動更新資料（電腦需開機）。
  echo 取消方式：再雙擊「取消每日自動更新.bat」。
) else (
  echo 設定失敗，請改以系統管理員身分執行本檔案。
)
pause

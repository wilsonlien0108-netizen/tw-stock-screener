@echo off
chcp 65001 >nul
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo 需要系統管理員權限，請在跳出的視窗按「是」…
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
netsh advfirewall firewall delete rule name="TW Stock Screener" >nul 2>&1
netsh advfirewall firewall add rule name="TW Stock Screener" dir=in action=allow protocol=TCP localport=8501
echo.
echo 完成！其他設備現在可以連進來了。可關閉此視窗。
pause

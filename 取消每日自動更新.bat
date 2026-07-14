@echo off
chcp 65001 >nul
schtasks /Delete /F /TN "TWStockScreenerDaily"
echo 已取消每日自動更新。
pause

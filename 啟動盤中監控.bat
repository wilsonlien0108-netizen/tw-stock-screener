@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 盤中K棒監控
echo   ★ 盤中 K 棒監控執行中（收盤 13:32 自動結束，關閉視窗即停止）
echo   ★ 監控條件與清單在篩選器「資料與設定 → 盤中K棒監控」調整
echo.
runtime\python.exe -X utf8 scripts\intraday_monitor.py
pause

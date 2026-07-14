@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 台股均線籌碼篩選器
echo.
echo   ★ 正在啟動台股篩選器，瀏覽器將自動開啟（約 5～10 秒）…
echo   ★ 使用完畢直接關閉本視窗即可。
echo.
start "" /min cmd /c "timeout /t 6 /nobreak >nul & start http://localhost:8501"
runtime\python.exe -X utf8 -m streamlit run app.py --server.headless true
pause

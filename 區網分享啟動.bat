@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 台股篩選器（區網分享模式）
for /f %%a in ('powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.InterfaceAlias -notmatch 'Loopback' -and $_.IPAddress -notmatch '^169\.254'} | Select-Object -First 1).IPAddress"') do set MYIP=%%a
echo.
echo   ★ 本機開啟：  http://localhost:8501
echo   ★ 其他設備：  http://%MYIP%:8501  （手機/平板需連同一個 Wi-Fi）
echo.
echo   ★ 第一次分享前，請先雙擊「設定防火牆放行.bat」一次。
echo   ★ 使用完畢直接關閉本視窗即可。
echo.
runtime\python.exe -X utf8 -m streamlit run app.py --server.address 0.0.0.0 --server.headless true
pause

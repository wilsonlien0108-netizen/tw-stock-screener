@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 台股篩選器（公開通道模式）
if not exist cloudflared.exe (
  echo   ★ 首次使用：下載 Cloudflare 通道程式（約 17MB，來源為 Cloudflare 官方 GitHub）…
  powershell -NoProfile -Command "Invoke-WebRequest 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile cloudflared.exe"
  if not exist cloudflared.exe (
    echo   下載失敗，請檢查網路後重試。
    pause
    exit /b
  )
)
echo.
echo   ★ 正在啟動篩選器與公開通道…
echo   ★ 下方出現 https://XXXX.trycloudflare.com 網址後，把它傳給家人即可。
echo   ★ 注意：每次重新啟動網址都會變，需要重傳；電腦關機或關閉視窗後家人就連不上。
echo.
start "台股篩選器伺服器" /min cmd /c "runtime\python.exe -X utf8 -m streamlit run app.py --server.headless true"
cloudflared.exe tunnel --url http://localhost:8501
pause

# 以「區網分享模式」啟動篩選器：同一 Wi-Fi/區網下的手機、平板、其他電腦
# 都可以用瀏覽器開 http://<本機IP>:8501 使用。
#
# 首次使用前，先以「系統管理員」執行一次防火牆放行：
#   New-NetFirewallRule -DisplayName "TW Stock Screener" -Direction Inbound `
#       -Protocol TCP -LocalPort 8501 -Action Allow

$projectDir = Split-Path -Parent $PSScriptRoot
$ip = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.InterfaceAlias -notmatch 'Loopback' -and $_.IPAddress -notmatch '^169\.254' } |
    Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "  本機開啟：  http://localhost:8501"
Write-Host "  其他設備：  http://${ip}:8501  （需在同一區網）"
Write-Host ""

Set-Location $projectDir
py -X utf8 -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501 --server.headless true

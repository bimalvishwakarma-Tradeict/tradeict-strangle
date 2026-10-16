$repo = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $repo
$port = 8700
if ($env:CHART_APP_PORT) {
    $port = [int]$env:CHART_APP_PORT
}
$url  = "http://127.0.0.1:$port"
function Test-Up {
    try {
        (Invoke-WebRequest "$url/api/strategies" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200
    } catch { $false }
}
if (-not (Test-Up)) {
    Start-Process powershell -ArgumentList @('-NoExit', '-Command', "Set-Location -LiteralPath '$repo'; `$host.UI.RawUI.WindowTitle='Tradeict Chart Server'; python chart_app\server.py")
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Up) { $ok = $true; break }
    }
    if (-not $ok) {
        Write-Host "Chart server 60s me start nahi hua. 'Tradeict Chart Server' window ka error dekho."
        Read-Host "Enter dabao"
        exit 1
    }
}
Start-Process $url

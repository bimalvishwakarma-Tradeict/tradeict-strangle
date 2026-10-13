$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path (Join-Path $Root "chart_app\server.py"))) {
  $Root = (Get-Location).Path
}
Set-Location $Root
python -m pip install playwright -q
python -m playwright install chromium
python chart_app\tests\run_e2e.py
exit $LASTEXITCODE

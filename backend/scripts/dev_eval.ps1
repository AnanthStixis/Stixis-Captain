# Restart ONLY the gateway (port 8000) with live evaluation on or off. Development only.
#   .\scripts\dev_eval.ps1 on     mock guard + mock judge, every Tier 1-2 call judged
#   .\scripts\dev_eval.ps1 off    back to normal (no guard, no evaluation)
# The mock guard and judge are served by the mock provider on port 8090 (no real model,
# no spend). Every other service keeps running; nothing in the database changes.
param([Parameter(Mandatory = $true)][ValidateSet("on", "off")][string]$Mode)
$ErrorActionPreference = "Stop"
$backend = Split-Path -Parent $PSScriptRoot
Set-Location $backend
$py = Join-Path $backend ".venv\Scripts\python.exe"
$logs = Join-Path $backend ".dev\logs"

foreach ($port in 8000, 8090) {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($c) { Stop-Process -Id $c[0].OwningProcess -Force }
}
Start-Sleep -Seconds 2
# The mock provider (also the mock guard and judge) is restarted so it runs the current code.
Start-Process -FilePath $py -WindowStyle Hidden -WorkingDirectory $backend `
    -ArgumentList @("-m", "uvicorn", "captain.devprovider:app", "--host", "127.0.0.1", "--port", "8090") `
    -RedirectStandardOutput (Join-Path $logs "provider.log") -RedirectStandardError (Join-Path $logs "provider.err.log")
Write-Host "restarted mock provider; restarting gateway"

$names = "CAPTAIN_GUARD_URLS", "CAPTAIN_JUDGE_URL", "CAPTAIN_JUDGE_MODEL", "CAPTAIN_EVAL_SAMPLE_RATE"
foreach ($n in $names) { Remove-Item "Env:$n" -ErrorAction SilentlyContinue }
if ($Mode -eq "on") {
    $env:CAPTAIN_GUARD_URLS = "http://127.0.0.1:8090"
    $env:CAPTAIN_JUDGE_URL = "http://127.0.0.1:8090"
    $env:CAPTAIN_JUDGE_MODEL = "mock-judge"
    $env:CAPTAIN_EVAL_SAMPLE_RATE = "1"
}
Start-Process -FilePath $py -WindowStyle Hidden -WorkingDirectory $backend `
    -ArgumentList @("-m", "uvicorn", "--factory", "captain.api.gateway_main:create_gateway_app_from_env",
                    "--host", "127.0.0.1", "--port", "8000") `
    -RedirectStandardOutput (Join-Path $logs "gateway.log") -RedirectStandardError (Join-Path $logs "gateway.err.log")
foreach ($n in $names) { Remove-Item "Env:$n" -ErrorAction SilentlyContinue }

for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        if ((Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/health" -TimeoutSec 2).StatusCode -eq 200) {
            Write-Host "gateway up, live evaluation $Mode"
            exit 0
        }
    } catch { }
}
Write-Host "gateway did not answer; see .dev\logs\gateway.err.log"
exit 1

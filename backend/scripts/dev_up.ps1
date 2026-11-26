# Start every Captain dev service in the background (logs in backend\.dev\logs).
#   .\scripts\dev_up.ps1            from the backend folder
# PostgreSQL must already be running (it is your own service). No Docker.
# Stop everything again with .\scripts\dev_down.ps1
$ErrorActionPreference = "Stop"
$backend = Split-Path -Parent $PSScriptRoot
Set-Location $backend
$py = Join-Path $backend ".venv\Scripts\python.exe"
$opa = Join-Path $backend "..\tools\opa\opa.exe"
$logs = Join-Path $backend ".dev\logs"
New-Item -ItemType Directory -Force $logs | Out-Null

function Start-Svc($name, $exe, $argList) {
    Start-Process -FilePath $exe -ArgumentList $argList -WorkingDirectory $backend -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs "$name.log") -RedirectStandardError (Join-Path $logs "$name.err.log")
    Write-Host "started $name"
}

Start-Svc "opa" $opa @("run", "--server", "--addr=127.0.0.1:8181", "..\policies")
Start-Svc "worker" $py @("-m", "captain.workers")
Start-Svc "provider" $py @("-m", "uvicorn", "captain.devprovider:app", "--host", "127.0.0.1", "--port", "8090")
Start-Sleep -Seconds 3
$apps = @(
    @("core", "captain.api.core_http:create_app_from_env", 8080),
    @("gateway", "captain.api.gateway_main:create_gateway_app_from_env", 8000),
    @("idp", "captain.devidp:create_dev_idp_from_env", 8200),
    @("admin", "captain.api.admin_main:create_admin_app_from_env", 8100),
    @("devmcp", "captain.devmcp:create_dev_mcp_from_env", 8310),
    @("mcp", "captain.api.mcp_main:create_mcp_app_from_env", 8300)
)
foreach ($a in $apps) {
    Start-Svc $a[0] $py @("-m", "uvicorn", "--factory", $a[1], "--host", "127.0.0.1", "--port", "$($a[2])")
}
Start-Sleep -Seconds 9
foreach ($p in 8181, 8080, 8000, 8100, 8300) {
    try { $code = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$p/health" -TimeoutSec 3).StatusCode } catch { $code = "no answer" }
    Write-Host "port ${p}: $code"
}
Write-Host "Dashboard: http://127.0.0.1:8100   Gateway: http://127.0.0.1:8000"

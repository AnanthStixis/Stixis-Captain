# Stop every Captain dev service (PostgreSQL is yours and is left running).
$ports = 8000, 8080, 8090, 8100, 8200, 8181, 8300, 8310, 8101, 8001
foreach ($p in $ports) {
    $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conn) {
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
        Write-Host "stopped port $p"
    }
}
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'captain\.workers' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "stopped worker $($_.ProcessId)" }
Write-Host "done"

$connections = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue

foreach ($connection in $connections) {
    Stop-Process -Id $connection.OwningProcess -Force -ErrorAction SilentlyContinue
}

# FuelRoute API - Quick Test Runner
# Start server and run Newman tests

Write-Host "=== FuelRoute API Test Runner ===" -ForegroundColor Cyan
Write-Host "Starting Django server on port 8001..." -ForegroundColor Green

# Start Django server in background
$serverProcess = Start-Process -PassThru -NoNewWindow -FilePath "python" -ArgumentList "-m uvicorn fuel_route_api.asgi:application --port 8001 --reload"

# Wait for server to be ready
Write-Host "Waiting for server to start..." -ForegroundColor Yellow
$retries = 0
$maxRetries = 30
do {
    try {
        $null = Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/health" -ErrorAction Stop
        Write-Host "Server is ready!" -ForegroundColor Green
        break
    } catch {
        $retries++
        if ($retries -ge $maxRetries) {
            Write-Host "Server failed to start after $maxRetries seconds" -ForegroundColor Red
            Stop-Process -InputObject $serverProcess
            exit 1
        }
        Start-Sleep -Seconds 1
    }
}

# Run Newman tests
Write-Host ""
Write-Host "Running Postman tests with Newman..." -ForegroundColor Green
$timestamp = Get-Date -Format "yyyy-MM-dd-HH-mm-ss"
$reportFile = "newman-report-$timestamp.html"

npx newman run -c FuelRoute.postman_collection.json `
  -e FuelRoute-Env.postman_environment.json `
  -r cli,html `
  -H $reportFile `
  --timeout-request 5000

$testExit = $LASTEXITCODE

# Stop server
Write-Host ""
Write-Host "Stopping server..." -ForegroundColor Yellow
Stop-Process -InputObject $serverProcess -Force

if ($testExit -eq 0) {
    Write-Host "Tests passed! Report saved to: $reportFile" -ForegroundColor Green
    # Try to open report in browser
    if (Test-Path $reportFile) {
        Invoke-Item $reportFile
    }
} else {
    Write-Host "Tests failed. Report saved to: $reportFile" -ForegroundColor Red
}

exit $testExit

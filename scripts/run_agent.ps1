param(
    [string]$EnvName = "apple"
)

$ErrorActionPreference = "Stop"

if (-not $env:LATSIM_API_URL) { throw "LATSIM_API_URL is required." }
if (-not $env:LATSIM_WORKER_ID) { throw "LATSIM_WORKER_ID is required." }
if (-not $env:LATSIM_WORKER_KEY_ID) { throw "LATSIM_WORKER_KEY_ID is required." }
if (-not $env:LATSIM_WORKER_PRIVATE_KEY_PATH) {
    throw "LATSIM_WORKER_PRIVATE_KEY_PATH is required."
}

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

Write-Host "Starting APPLE outbound agent..."
Write-Host "LatSim API: $env:LATSIM_API_URL"
Write-Host "Worker ID: $env:LATSIM_WORKER_ID"
Write-Host "Concurrency: 1"

conda run -n $EnvName --no-capture-output python agent.py

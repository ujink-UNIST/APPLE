param(
    [string]$EnvName = "apple",
    [string]$SettingsPath = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $SettingsPath) {
    $SettingsPath = Join-Path $ProjectRoot "secrets\agent-settings.json"
}
if (Test-Path $SettingsPath -PathType Leaf) {
    $settings = Get-Content $SettingsPath -Raw | ConvertFrom-Json
    if ($settings.schema_version -ne 1) {
        throw "Unsupported APPLE agent settings version."
    }
    $env:LATSIM_API_URL = $settings.api_url
    $env:LATSIM_WORKER_ID = $settings.worker_id
    $env:LATSIM_WORKER_KEY_ID = $settings.key_id
    $env:LATSIM_WORKER_PRIVATE_KEY_PATH = $settings.private_key_path
    $env:SSL_CERT_FILE = $settings.ssl_cert_file
    $env:ANSYS_EXE = $settings.ansys_exe
    $env:ANSYS_NP = "$($settings.ansys_np)"
    $env:ANSYS_VERSION = $settings.ansys_version
}

if (-not $env:LATSIM_API_URL) { throw "LATSIM_API_URL is required." }
if (-not $env:LATSIM_WORKER_ID) { throw "LATSIM_WORKER_ID is required." }
if (-not $env:LATSIM_WORKER_KEY_ID) { throw "LATSIM_WORKER_KEY_ID is required." }
if (-not $env:LATSIM_WORKER_PRIVATE_KEY_PATH) {
    throw "LATSIM_WORKER_PRIVATE_KEY_PATH is required."
}
if (-not (Test-Path $env:LATSIM_WORKER_PRIVATE_KEY_PATH -PathType Leaf)) {
    throw "APPLE worker private key was not found. Run scripts\set_agent.ps1 first."
}

Set-Location $ProjectRoot
Write-Host "Starting APPLE outbound agent..."
Write-Host "LatSim API: $env:LATSIM_API_URL"
Write-Host "Worker ID: $env:LATSIM_WORKER_ID"
Write-Host "Concurrency: 1"

conda run -n $EnvName --no-capture-output python agent.py

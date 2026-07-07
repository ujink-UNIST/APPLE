param(
    [string]$EnvName = "apple",
    [string]$HostName = "127.0.0.1",
    [int]$Port = 49913,
    [switch]$Reload,
    [string]$ApiKey = $env:APPLE_API_KEY,
    [string]$CertFile = "",
    [string]$KeyFile = ""
)

$ErrorActionPreference = "Stop"

function Find-Conda {
    $cmd = Get-Command conda -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        "$env:USERPROFILE\miniconda3\Scripts\conda.exe",
        "$env:USERPROFILE\anaconda3\Scripts\conda.exe",
        "$env:LOCALAPPDATA\miniconda3\Scripts\conda.exe",
        "$env:LOCALAPPDATA\anaconda3\Scripts\conda.exe",
        "C:\ProgramData\miniconda3\Scripts\conda.exe",
        "C:\ProgramData\anaconda3\Scripts\conda.exe"
    )

    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }

    throw "conda를 찾을 수 없습니다. Miniconda/Anaconda를 설치하거나 conda를 PATH에 추가하세요."
}

$conda = Find-Conda
Write-Host "Using conda: $conda"

$envList = & $conda env list --json | ConvertFrom-Json
$exists = $false
foreach ($envPath in $envList.envs) {
    if ((Split-Path $envPath -Leaf) -eq $EnvName) {
        $exists = $true
        break
    }
}

if (-not $exists) {
    Write-Host "Creating conda env '$EnvName' from environment.yml..."
    & $conda env create -f environment.yml
} else {
    Write-Host "Conda env '$EnvName' already exists. Updating dependencies..."
    & $conda env update -n $EnvName -f environment.yml --prune
}

if ($ApiKey) {
    $env:APPLE_API_KEY = $ApiKey
    Write-Host "API key protection: enabled (X-API-Key required)"
} else {
    Write-Host "API key protection: disabled (set -ApiKey or APPLE_API_KEY to enable)"
}

$uvicornArgs = @(
    "-m", "uvicorn", "app:app",
    "--host", $HostName,
    "--port", "$Port",
    "--log-level", "info",
    "--access-log"
)
if ($CertFile -and $KeyFile) {
    $uvicornArgs += @("--ssl-certfile", $CertFile, "--ssl-keyfile", $KeyFile)
}
if ($Reload) {
    $uvicornArgs += "--reload"
}

Write-Host ""
$scheme = if ($CertFile -and $KeyFile) { "https" } else { "http" }
Write-Host "Starting APPLE API on $scheme://$HostName`:$Port"
Write-Host "Swagger UI: $scheme://$HostName`:$Port/docs"
Write-Host "OpenAPI:    $scheme://$HostName`:$Port/openapi.json"
Write-Host "Press Ctrl+C to stop."
Write-Host ""

# --no-capture-output가 없으면 conda run이 uvicorn 로그를 숨기거나 늦게 출력할 수 있습니다.
& $conda run -n $EnvName --no-capture-output python @uvicornArgs

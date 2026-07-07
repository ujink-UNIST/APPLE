param(
    [string]$EnvName = "apple",
    [string]$HostName = "127.0.0.1",
    [int]$Port = 49913,
    [switch]$Reload
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

$uvicornArgs = @(
    "-m", "uvicorn", "app:app",
    "--host", $HostName,
    "--port", "$Port",
    "--log-level", "info",
    "--access-log"
)
if ($Reload) {
    $uvicornArgs += "--reload"
}

Write-Host ""
Write-Host "Starting APPLE API on http://$HostName`:$Port"
Write-Host "Swagger UI: http://$HostName`:$Port/docs"
Write-Host "OpenAPI:    http://$HostName`:$Port/openapi.json"
Write-Host "Press Ctrl+C to stop."
Write-Host ""

# --no-capture-output가 없으면 conda run이 uvicorn 로그를 숨기거나 늦게 출력할 수 있습니다.
& $conda run -n $EnvName --no-capture-output python @uvicornArgs

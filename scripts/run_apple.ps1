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

$uvicornArgs = @("-m", "uvicorn", "app:app", "--host", $HostName, "--port", "$Port")
if ($Reload) {
    $uvicornArgs += "--reload"
}

Write-Host "Starting APPLE API on http://$HostName`:$Port"
& $conda run -n $EnvName python @uvicornArgs

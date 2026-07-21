param(
    [string]$BackendIp = "10.74.19.162",
    [string]$BackendHost = "latsim-backend",
    [string]$ApiUrl = "https://latsim-backend",
    [string]$WorkerId = "ansys-workstation-01",
    [string]$KeyId = "key-1",
    [Parameter(Mandatory = $true)]
    [string]$RootCertPath,
    [string]$EnvName = "apple",
    [string]$AnsysExe = "C:\Program Files\ANSYS Inc\ANSYS Student\v252\ANSYS\bin\winx64\ANSYS252.exe",
    [ValidateRange(1, 256)]
    [int]$AnsysNp = 2,
    [string]$AnsysVersion = "2025 R2.04"
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an administrator PowerShell."
}

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptRoot
$SecretsDir = Join-Path $ProjectRoot "secrets"
$PrivateKeyPath = Join-Path $SecretsDir "worker-key.pem"
$InstalledRootCertPath = Join-Path $SecretsDir "caddy-root.crt"
$SettingsPath = Join-Path $SecretsDir "agent-settings.json"
$PublicKeyPath = Join-Path $SecretsDir "worker-public-key.json"

if (-not (Test-Path $RootCertPath -PathType Leaf)) {
    throw "Caddy root certificate not found: $RootCertPath"
}
if (-not (Test-Path $AnsysExe -PathType Leaf)) {
    throw "ANSYS executable not found: $AnsysExe"
}
if ($ApiUrl -notmatch '^https://') {
    throw "ApiUrl must use HTTPS."
}
if ($WorkerId -notmatch '^[A-Za-z0-9_.@-]{1,64}$') {
    throw "WorkerId contains unsupported characters."
}
if ($KeyId -notmatch '^[A-Za-z0-9_.-]{1,64}$') {
    throw "KeyId contains unsupported characters."
}

function Find-Conda {
    $command = Get-Command conda -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    $candidates = @(
        "$env:USERPROFILE\miniconda3\Scripts\conda.exe",
        "$env:USERPROFILE\anaconda3\Scripts\conda.exe",
        "$env:LOCALAPPDATA\miniconda3\Scripts\conda.exe",
        "$env:LOCALAPPDATA\anaconda3\Scripts\conda.exe",
        "C:\ProgramData\miniconda3\Scripts\conda.exe",
        "C:\ProgramData\anaconda3\Scripts\conda.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate -PathType Leaf) { return $candidate }
    }
    throw "conda was not found. Install Miniconda or add conda to PATH."
}

$Conda = Find-Conda

Set-Location $ProjectRoot
New-Item -ItemType Directory -Force $SecretsDir | Out-Null
Copy-Item -Force $RootCertPath $InstalledRootCertPath

$certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new(
    $InstalledRootCertPath
)
$installed = Get-ChildItem Cert:\LocalMachine\Root |
    Where-Object Thumbprint -eq $certificate.Thumbprint
if (-not $installed) {
    Import-Certificate `
        -FilePath $InstalledRootCertPath `
        -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
}

$hostsPath = "$env:SystemRoot\System32\drivers\etc\hosts"
$escapedHost = [Regex]::Escape($BackendHost)
if (-not (Select-String -Path $hostsPath -Pattern "^\s*$BackendIp\s+$escapedHost\s*$" -Quiet)) {
    Add-Content -Path $hostsPath -Value "`n$BackendIp  $BackendHost"
}

Write-Host "Updating conda environment '$EnvName'..."
& $Conda env update -n $EnvName -f (Join-Path $ProjectRoot "environment.yml") --prune
if ($LASTEXITCODE -ne 0) {
    throw "Failed to update conda environment '$EnvName'."
}

$publicKeyOutput = & $Conda run -n $EnvName python `
    (Join-Path $ScriptRoot "generate_worker_key.py") $PrivateKeyPath
if ($LASTEXITCODE -ne 0) {
    throw "Failed to create or read the Ed25519 worker key."
}
$PublicKey = ($publicKeyOutput | Select-Object -Last 1).Trim()
if ($PublicKey -notmatch '^[A-Za-z0-9_-]{43}$') {
    throw "Worker public key output is invalid."
}

$settings = [ordered]@{
    schema_version = 1
    api_url = $ApiUrl
    worker_id = $WorkerId
    key_id = $KeyId
    private_key_path = $PrivateKeyPath
    ssl_cert_file = $InstalledRootCertPath
    ansys_exe = $AnsysExe
    ansys_np = $AnsysNp
    ansys_version = $AnsysVersion
}
$settingsJson = $settings | ConvertTo-Json -Compress
[IO.File]::WriteAllText(
    $SettingsPath,
    $settingsJson + "`n",
    [Text.UTF8Encoding]::new($false)
)

$publicRegistration = [ordered]@{}
$publicRegistration[$WorkerId] = [ordered]@{}
$publicRegistration[$WorkerId][$KeyId] = $PublicKey
$publicRegistrationJson = $publicRegistration | ConvertTo-Json -Compress
[IO.File]::WriteAllText(
    $PublicKeyPath,
    $publicRegistrationJson + "`n",
    [Text.UTF8Encoding]::new($false)
)

$env:SSL_CERT_FILE = $InstalledRootCertPath
Write-Host "Testing LatSim HTTPS endpoint..."
$response = Invoke-RestMethod -Uri "$ApiUrl/capabilities" -TimeoutSec 15
if ($response.kind -ne "latsim_api_capabilities") {
    throw "LatSim capabilities response is invalid."
}

Write-Host ""
Write-Host "APPLE agent setup completed."
Write-Host "Settings: $SettingsPath"
Write-Host "Private key: $PrivateKeyPath (do not copy)"
Write-Host "Public registration file: $PublicKeyPath"
Write-Host ""
Write-Host "Merge this public mapping into LATSIM_WORKER_PUBLIC_KEYS on the Backend:"
Write-Host $publicRegistrationJson
Write-Host ""
Write-Host "After restarting the Backend with that public key, run:"
Write-Host ".\scripts\run_agent.ps1"

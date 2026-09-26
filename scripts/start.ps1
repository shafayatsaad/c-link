param(
    [string]$BindAddress = "127.0.0.1",
    [int]$Port = 0,
    [string]$ModelUrl = ""
)

$ErrorActionPreference = "Stop"
$productionRoot = Split-Path -Parent $PSScriptRoot
Set-Location $productionRoot

$localConfig = Join-Path $productionRoot "config\local.ps1"
if (Test-Path -LiteralPath $localConfig) {
    . $localConfig
}

if ($ModelUrl) {
    $env:C_LINK_LLAMACPP_URL = $ModelUrl
} elseif (-not $env:C_LINK_LLAMACPP_URL) {
    $env:C_LINK_LLAMACPP_URL = "http://127.0.0.1:9932"
}

if ($Port -eq 0) {
    if ($env:C_LINK_PORT) {
        $Port = [int]$env:C_LINK_PORT
    } else {
        $Port = 9931
    }
}

$cli = Join-Path $productionRoot ".venv\Scripts\c-link.exe"
if (-not (Test-Path -LiteralPath $cli)) {
    throw "C-Link is not installed in .venv. Run: py -3.13 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e ."
}

if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $owner = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        $ownerName = if ($owner) { $owner.ProcessName } else { "unknown process" }
        throw "Port $Port is already used by PID $($listener.OwningProcess) ($ownerName). Choose another C-Link port; do not stop a model server you need."
    }
}

& $cli run --host $BindAddress --port $Port

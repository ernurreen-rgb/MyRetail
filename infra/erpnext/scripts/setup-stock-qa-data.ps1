[CmdletBinding()]
param(
    [string]$ErpEnvFile = "infra/erpnext/.env"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "../../..")).Path
$envPath = if ([IO.Path]::IsPathRooted($ErpEnvFile)) {
    $ErpEnvFile
}
else {
    Join-Path $root $ErpEnvFile
}
$scriptPath = Join-Path $PSScriptRoot "setup-stock-qa-data.py"

$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) {
    throw "Python is required to prepare ERPNext QA data."
}

& $python.Source $scriptPath --env-file $envPath
if ($LASTEXITCODE -ne 0) {
    throw "ERPNext QA data setup failed with exit code $LASTEXITCODE."
}

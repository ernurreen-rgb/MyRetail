[CmdletBinding()]
param(
    [string]$ComposeFile = "infra/erpnext/compose.yaml",
    [string]$ErpEnvFile = "infra/erpnext/.env",
    [string]$ApiEnvFile = "services/api/.env"
)

$ErrorActionPreference = "Stop"

function Read-DotEnv {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Environment file not found: $Path"
    }

    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }

        $parts = $trimmed.Split("=", 2)
        if ($parts.Count -eq 2) {
            $values[$parts[0].Trim()] = $parts[1].Trim()
        }
    }

    return $values
}

function Invoke-ErpRequest {
    param(
        [Parameter(Mandatory)][ValidateSet("Get", "Post")][string]$Method,
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][Microsoft.PowerShell.Commands.WebRequestSession]$Session,
        [object]$Body
    )

    $parameters = @{
        Method     = $Method
        Uri        = $Uri
        WebSession = $Session
        ContentType = "application/json"
    }
    if ($null -ne $Body) {
        $parameters.Body = $Body | ConvertTo-Json -Depth 8 -Compress
    }

    return Invoke-RestMethod @parameters
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "../../..")).Path
$erpEnvPath = Join-Path $root $ErpEnvFile
$apiEnvPath = Join-Path $root $ApiEnvFile
$erpEnv = Read-DotEnv -Path $erpEnvPath

foreach ($required in @("SITE_NAME", "HTTP_PORT", "ADMIN_PASSWORD")) {
    if (-not $erpEnv.ContainsKey($required) -or -not $erpEnv[$required]) {
        throw "Required variable $required is missing from $ErpEnvFile"
    }
}

$baseUrl = "http://$($erpEnv.SITE_NAME):$($erpEnv.HTTP_PORT)"
$serviceUser = "myretail-api@local.test"
$serviceRole = "MyRetail API Reader"
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession

Invoke-RestMethod `
    -Method Post `
    -Uri "$baseUrl/api/method/login" `
    -WebSession $session `
    -ContentType "application/x-www-form-urlencoded" `
    -Body @{ usr = "Administrator"; pwd = $erpEnv.ADMIN_PASSWORD } | Out-Null

$encodedRole = [Uri]::EscapeDataString($serviceRole)
try {
    Invoke-ErpRequest -Method Get -Uri "$baseUrl/api/resource/Role/$encodedRole" -Session $session | Out-Null
}
catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw }
    Invoke-ErpRequest -Method Post -Uri "$baseUrl/api/resource/Role" -Session $session -Body @{
        role_name = $serviceRole
        desk_access = 0
    } | Out-Null
}

$permissionFilters = [Uri]::EscapeDataString((@{
    parent = "Item"
    role = $serviceRole
    permlevel = 0
} | ConvertTo-Json -Compress))
$permissionFields = [Uri]::EscapeDataString('["name"]')
$permissions = Invoke-ErpRequest `
    -Method Get `
    -Uri "$baseUrl/api/resource/Custom%20DocPerm?filters=$permissionFilters&fields=$permissionFields&limit_page_length=1" `
    -Session $session

if (-not $permissions.data -or $permissions.data.Count -eq 0) {
    Invoke-ErpRequest -Method Post -Uri "$baseUrl/api/resource/Custom%20DocPerm" -Session $session -Body @{
        parent = "Item"
        role = $serviceRole
        permlevel = 0
        read = 1
        select = 1
    } | Out-Null
}

$encodedUser = [Uri]::EscapeDataString($serviceUser)
try {
    Invoke-ErpRequest -Method Get -Uri "$baseUrl/api/resource/User/$encodedUser" -Session $session | Out-Null
}
catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw }
    Invoke-ErpRequest -Method Post -Uri "$baseUrl/api/resource/User" -Session $session -Body @{
        email = $serviceUser
        first_name = "MyRetail API"
        enabled = 1
        user_type = "System User"
        send_welcome_email = 0
        roles = @(@{ role = $serviceRole })
    } | Out-Null
}

$keys = Invoke-RestMethod `
    -Method Post `
    -Uri "$baseUrl/api/method/frappe.core.doctype.user.user.generate_keys" `
    -WebSession $session `
    -ContentType "application/x-www-form-urlencoded" `
    -Body @{ user = $serviceUser }

if (-not $keys.message.api_key -or -not $keys.message.api_secret) {
    throw "ERPNext did not return service user API keys"
}

$apiEnv = @(
    "MYRETAIL_ENVIRONMENT=development"
    "MYRETAIL_LOG_LEVEL=INFO"
    "MYRETAIL_ERPNEXT_BASE_URL=$baseUrl"
    "MYRETAIL_ERPNEXT_API_KEY=$($keys.message.api_key)"
    "MYRETAIL_ERPNEXT_API_SECRET=$($keys.message.api_secret)"
    "MYRETAIL_ERPNEXT_TIMEOUT_SECONDS=10"
) -join [Environment]::NewLine

$apiEnvDirectory = Split-Path -Parent $apiEnvPath
[System.IO.Directory]::CreateDirectory($apiEnvDirectory) | Out-Null
[System.IO.File]::WriteAllText($apiEnvPath, $apiEnv + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))

Write-Output "ERPNext service user configured. Secrets were written only to $ApiEnvFile."

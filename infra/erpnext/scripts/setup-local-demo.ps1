[CmdletBinding()]
param(
    [string]$ErpEnvFile = "infra/erpnext/.env"
)

$ErrorActionPreference = "Stop"

function Read-DotEnv {
    param([Parameter(Mandatory)][string]$Path)

    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $parts = $trimmed.Split("=", 2)
        if ($parts.Count -eq 2) { $values[$parts[0].Trim()] = $parts[1].Trim() }
    }
    return $values
}

function Get-Resource {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][Microsoft.PowerShell.Commands.WebRequestSession]$Session
    )
    return Invoke-RestMethod -Method Get -Uri $Uri -WebSession $Session -TimeoutSec 60
}

function Set-FrappeValue {
    param(
        [Parameter(Mandatory)][string]$BaseUrl,
        [Parameter(Mandatory)][Microsoft.PowerShell.Commands.WebRequestSession]$Session,
        [Parameter(Mandatory)][string]$Doctype,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$FieldName,
        [Parameter(Mandatory)][string]$Value
    )

    Invoke-RestMethod `
        -Method Post `
        -Uri "$BaseUrl/api/method/frappe.client.set_value" `
        -WebSession $Session `
        -ContentType "application/x-www-form-urlencoded" `
        -Body @{
            doctype = $Doctype
            name = $Name
            fieldname = $FieldName
            value = $Value
        } `
        -TimeoutSec 60 | Out-Null
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "../../..")).Path
$erpEnvPath = Join-Path $root $ErpEnvFile
$erpEnv = Read-DotEnv -Path $erpEnvPath

foreach ($required in @("SITE_NAME", "HTTP_PORT", "ADMIN_PASSWORD")) {
    if (-not $erpEnv.ContainsKey($required) -or -not $erpEnv[$required]) {
        throw "Required variable $required is missing from $ErpEnvFile"
    }
}

$baseUrl = "http://$($erpEnv.SITE_NAME):$($erpEnv.HTTP_PORT)"
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession

Invoke-RestMethod `
    -Method Post `
    -Uri "$baseUrl/api/method/login" `
    -WebSession $session `
    -ContentType "application/x-www-form-urlencoded" `
    -Body @{ usr = "Administrator"; pwd = $erpEnv.ADMIN_PASSWORD } | Out-Null

Set-FrappeValue `
    -BaseUrl $baseUrl `
    -Session $session `
    -Doctype "System Settings" `
    -Name "System Settings" `
    -FieldName "language" `
    -Value "ru"

Set-FrappeValue `
    -BaseUrl $baseUrl `
    -Session $session `
    -Doctype "User" `
    -Name "Administrator" `
    -FieldName "language" `
    -Value "ru"

$companyName = "MyRetail Demo"
$encodedCompany = [Uri]::EscapeDataString($companyName)
$companyExists = $true
try {
    Get-Resource -Uri "$baseUrl/api/resource/Company/$encodedCompany" -Session $session | Out-Null
}
catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw }
    $companyExists = $false
}

if (-not $companyExists) {
    $setupArgs = @{
        language = "Russian"
        country = "Kazakhstan"
        timezone = "Asia/Qyzylorda"
        currency = "KZT"
        company_name = $companyName
        company_abbr = "MRD"
        domain = "Retail"
        chart_of_accounts = "Standard"
        fy_start_date = "2026-01-01"
        fy_end_date = "2026-12-31"
        enable_telemetry = 0
        setup_demo = 0
    } | ConvertTo-Json -Compress

    $setupResult = Invoke-RestMethod `
        -Method Post `
        -Uri "$baseUrl/api/method/frappe.desk.page.setup_wizard.setup_wizard.setup_complete" `
        -WebSession $session `
        -ContentType "application/x-www-form-urlencoded" `
        -Body @{ args = $setupArgs } `
        -TimeoutSec 600

    if ($setupResult.message.status -ne "ok") {
        throw "ERPNext setup wizard did not complete successfully"
    }
}

$itemCode = "DEMO-001"
$encodedItem = [Uri]::EscapeDataString($itemCode)
$itemExists = $true
try {
    Get-Resource -Uri "$baseUrl/api/resource/Item/$encodedItem" -Session $session | Out-Null
}
catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw }
    $itemExists = $false
}

if (-not $itemExists) {
    $fields = [Uri]::EscapeDataString('["name"]')
    $filters = [Uri]::EscapeDataString('[["Item Group","is_group","=",0]]')
    $groups = Get-Resource `
        -Uri "$baseUrl/api/resource/Item%20Group?fields=$fields&filters=$filters&limit_page_length=1" `
        -Session $session
    if (-not $groups.data -or $groups.data.Count -eq 0) {
        throw "ERPNext has no leaf Item Group after setup"
    }

    $itemBody = @{
        item_code = $itemCode
        item_name = "MyRetail Demo Product"
        description = "Local development product"
        item_group = $groups.data[0].name
        stock_uom = "Nos"
        is_stock_item = 1
        disabled = 0
    } | ConvertTo-Json -Compress

    Invoke-RestMethod `
        -Method Post `
        -Uri "$baseUrl/api/resource/Item" `
        -WebSession $session `
        -ContentType "application/json" `
        -Body $itemBody `
        -TimeoutSec 60 | Out-Null
}

Write-Output "Local ERPNext demo company and product are ready."

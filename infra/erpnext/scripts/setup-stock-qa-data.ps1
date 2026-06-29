[CmdletBinding()]
param(
    [string]$ErpEnvFile = "infra/erpnext/.env",
    [string]$CompanyName = "MyRetail Demo"
)

$ErrorActionPreference = "Stop"
$ReservationMarker = "MYRETAIL-QA-RESERVATION"

function ConvertFrom-Utf8Base64 {
    param([Parameter(Mandatory)][string]$Value)
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value))
}

$MainWarehouse = ConvertFrom-Utf8Base64 "0J7RgdC90L7QstC90L7QuSDRgdC60LvQsNC0IFFBIC0gTVJE"
$ReserveWarehouse = ConvertFrom-Utf8Base64 "0KDQtdC30LXRgNCy0L3Ri9C5INGB0LrQu9Cw0LQgUUEgLSBNUkQ="

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

function ConvertTo-JsonText {
    param([Parameter(Mandatory)][object]$InputObject)
    return ConvertTo-Json -InputObject $InputObject -Depth 12 -Compress
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
        TimeoutSec = 120
    }
    if ($null -ne $Body) {
        $jsonBody = $Body | ConvertTo-Json -Depth 12 -Compress
        $parameters.ContentType = "application/json; charset=utf-8"
        $parameters.Body = [Text.Encoding]::UTF8.GetBytes($jsonBody)
    }
    Write-Verbose "$Method $Uri"
    return Invoke-RestMethod @parameters
}

function Get-ErpList {
    param(
        [Parameter(Mandatory)][string]$BaseUrl,
        [Parameter(Mandatory)][string]$Doctype,
        [Parameter(Mandatory)][object[]]$Fields,
        [Parameter(Mandatory)][object[]]$Filters,
        [Parameter(Mandatory)][Microsoft.PowerShell.Commands.WebRequestSession]$Session,
        [int]$Limit = 20
    )

    $encodedDoctype = [Uri]::EscapeDataString($Doctype)
    $fieldsJson = [Uri]::EscapeDataString((ConvertTo-JsonText -InputObject $Fields))
    $filtersJson = [Uri]::EscapeDataString((ConvertTo-JsonText -InputObject $Filters))
    return Invoke-ErpRequest `
        -Method Get `
        -Uri "$BaseUrl/api/resource/${encodedDoctype}?fields=$fieldsJson&filters=$filtersJson&limit_page_length=$Limit" `
        -Session $Session
}

function Test-ErpResourceExists {
    param(
        [Parameter(Mandatory)][string]$BaseUrl,
        [Parameter(Mandatory)][string]$Doctype,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][Microsoft.PowerShell.Commands.WebRequestSession]$Session
    )

    $encodedDoctype = [Uri]::EscapeDataString($Doctype)
    $encodedName = [Uri]::EscapeDataString($Name)
    try {
        Invoke-ErpRequest `
            -Method Get `
            -Uri "$BaseUrl/api/resource/$encodedDoctype/$encodedName" `
            -Session $Session | Out-Null
        return $true
    }
    catch {
        if ($_.Exception.Response.StatusCode.value__ -eq 404) { return $false }
        throw
    }
}

function Ensure-Warehouse {
    param(
        [Parameter(Mandatory)][string]$BaseUrl,
        [Parameter(Mandatory)][string]$WarehouseId,
        [Parameter(Mandatory)][string]$CompanyName,
        [Parameter(Mandatory)][Microsoft.PowerShell.Commands.WebRequestSession]$Session
    )

    if (Test-ErpResourceExists -BaseUrl $BaseUrl -Doctype "Warehouse" -Name $WarehouseId -Session $Session) {
        return
    }
    Invoke-ErpRequest `
        -Method Post `
        -Uri "$BaseUrl/api/resource/Warehouse" `
        -Session $Session `
        -Body @{
            warehouse_name = ($WarehouseId -replace " - .+$", "")
            company = $CompanyName
            is_group = 0
        } | Out-Null
}

function Get-LeafItemGroup {
    param(
        [Parameter(Mandatory)][string]$BaseUrl,
        [Parameter(Mandatory)][Microsoft.PowerShell.Commands.WebRequestSession]$Session
    )

    $groups = Get-ErpList `
        -BaseUrl $BaseUrl `
        -Doctype "Item Group" `
        -Fields @("name") `
        -Filters @(,@("Item Group", "is_group", "=", 0)) `
        -Session $Session `
        -Limit 1
    if (-not $groups.data -or $groups.data.Count -eq 0) {
        throw "ERPNext has no leaf Item Group"
    }
    return $groups.data[0].name
}

function Ensure-Item {
    param(
        [Parameter(Mandatory)][string]$BaseUrl,
        [Parameter(Mandatory)][hashtable]$Item,
        [Parameter(Mandatory)][string]$ItemGroup,
        [Parameter(Mandatory)][Microsoft.PowerShell.Commands.WebRequestSession]$Session
    )

    if (Test-ErpResourceExists -BaseUrl $BaseUrl -Doctype "Item" -Name $Item.code -Session $Session) {
        return
    }
    Invoke-ErpRequest `
        -Method Post `
        -Uri "$BaseUrl/api/resource/Item" `
        -Session $Session `
        -Body @{
            item_code = $Item.code
            item_name = $Item.name
            item_group = $ItemGroup
            stock_uom = $Item.uom
            is_stock_item = 1
            disabled = 0
            barcodes = @(@{ barcode = $Item.barcode })
        } | Out-Null
}

function Get-ActualQuantity {
    param(
        [Parameter(Mandatory)][string]$BaseUrl,
        [Parameter(Mandatory)][string]$ItemCode,
        [Parameter(Mandatory)][string]$WarehouseId,
        [Parameter(Mandatory)][Microsoft.PowerShell.Commands.WebRequestSession]$Session
    )

    $bin = Get-ErpList `
        -BaseUrl $BaseUrl `
        -Doctype "Bin" `
        -Fields @("actual_qty") `
        -Filters @(
            ,@("Bin", "item_code", "=", $ItemCode)
            ,@("Bin", "warehouse", "=", $WarehouseId)
        ) `
        -Session $Session `
        -Limit 1
    if (-not $bin.data -or $bin.data.Count -eq 0) {
        return [decimal]0
    }
    return [decimal]$bin.data[0].actual_qty
}

function Add-StockEntry {
    param(
        [Parameter(Mandatory)][string]$BaseUrl,
        [Parameter(Mandatory)][string]$Type,
        [Parameter(Mandatory)][string]$ItemCode,
        [Parameter(Mandatory)][string]$WarehouseId,
        [Parameter(Mandatory)][decimal]$Quantity,
        [Parameter(Mandatory)][Microsoft.PowerShell.Commands.WebRequestSession]$Session
    )

    if ($Quantity -le 0) { return }
    $item = @{
        item_code = $ItemCode
        qty = $Quantity.ToString("0.000", [Globalization.CultureInfo]::InvariantCulture)
    }
    if ($Type -eq "Material Receipt") {
        $item.t_warehouse = $WarehouseId
    }
    else {
        $item.s_warehouse = $WarehouseId
    }
    Invoke-ErpRequest `
        -Method Post `
        -Uri "$BaseUrl/api/resource/Stock%20Entry" `
        -Session $Session `
        -Body @{
            doctype = "Stock Entry"
            stock_entry_type = $Type
            purpose = $Type
            docstatus = 1
            remarks = "MyRetail stock QA seed"
            items = @($item)
        } | Out-Null
}

function Ensure-StockQuantity {
    param(
        [Parameter(Mandatory)][string]$BaseUrl,
        [Parameter(Mandatory)][string]$ItemCode,
        [Parameter(Mandatory)][string]$WarehouseId,
        [Parameter(Mandatory)][decimal]$TargetQuantity,
        [Parameter(Mandatory)][Microsoft.PowerShell.Commands.WebRequestSession]$Session
    )

    $actual = Get-ActualQuantity `
        -BaseUrl $BaseUrl `
        -ItemCode $ItemCode `
        -WarehouseId $WarehouseId `
        -Session $Session
    $delta = $TargetQuantity - $actual
    if ($delta -gt 0) {
        Add-StockEntry `
            -BaseUrl $BaseUrl `
            -Type "Material Receipt" `
            -ItemCode $ItemCode `
            -WarehouseId $WarehouseId `
            -Quantity $delta `
            -Session $Session
    }
    elseif ($delta -lt 0) {
        Add-StockEntry `
            -BaseUrl $BaseUrl `
            -Type "Material Issue" `
            -ItemCode $ItemCode `
            -WarehouseId $WarehouseId `
            -Quantity ([decimal]::Negate($delta)) `
            -Session $Session
    }
}

function Ensure-Customer {
    param(
        [Parameter(Mandatory)][string]$BaseUrl,
        [Parameter(Mandatory)][string]$CustomerName,
        [Parameter(Mandatory)][Microsoft.PowerShell.Commands.WebRequestSession]$Session
    )

    if (Test-ErpResourceExists -BaseUrl $BaseUrl -Doctype "Customer" -Name $CustomerName -Session $Session) {
        return
    }
    Invoke-ErpRequest `
        -Method Post `
        -Uri "$BaseUrl/api/resource/Customer" `
        -Session $Session `
        -Body @{
            customer_name = $CustomerName
            customer_type = "Individual"
            customer_group = "Individual"
            territory = "Kazakhstan"
        } | Out-Null
}

function Ensure-MilkReservation {
    param(
        [Parameter(Mandatory)][string]$BaseUrl,
        [Parameter(Mandatory)][string]$CustomerName,
        [Parameter(Mandatory)][Microsoft.PowerShell.Commands.WebRequestSession]$Session
    )

    $orders = Get-ErpList `
        -BaseUrl $BaseUrl `
        -Doctype "Sales Order" `
        -Fields @("name") `
        -Filters @(,@("Sales Order", "po_no", "=", $ReservationMarker)) `
        -Session $Session `
        -Limit 1
    if ($orders.data -and $orders.data.Count -gt 0) {
        return
    }
    Invoke-ErpRequest `
        -Method Post `
        -Uri "$BaseUrl/api/resource/Sales%20Order" `
        -Session $Session `
        -Body @{
            doctype = "Sales Order"
            customer = $CustomerName
            po_no = $ReservationMarker
            delivery_date = "2026-12-31"
            docstatus = 1
            items = @(
                @{
                    item_code = "QA-MILK-001"
                    qty = "2.000"
                    warehouse = $MainWarehouse
                    delivery_date = "2026-12-31"
                }
            )
        } | Out-Null
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
    -Body @{ usr = "Administrator"; pwd = $erpEnv.ADMIN_PASSWORD } `
    -TimeoutSec 120 | Out-Null

Ensure-Warehouse -BaseUrl $baseUrl -WarehouseId $MainWarehouse -CompanyName $CompanyName -Session $session
Ensure-Warehouse -BaseUrl $baseUrl -WarehouseId $ReserveWarehouse -CompanyName $CompanyName -Session $session

$itemGroup = Get-LeafItemGroup -BaseUrl $baseUrl -Session $session
$items = @(
    @{ code = "QA-MILK-001"; name = ConvertFrom-Utf8Base64 "0JzQvtC70L7QutC+IDMsMiU="; barcode = "4870000000011"; uom = "Nos"; main = [decimal]"10.000"; reserve = [decimal]"2.000" },
    @{ code = "QA-BREAD-001"; name = ConvertFrom-Utf8Base64 "0KXQu9C10LEg0L/RiNC10L3QuNGH0L3Ri9C5"; barcode = "4870000000028"; uom = "Nos"; main = [decimal]"5.000"; reserve = [decimal]"0.000" },
    @{ code = "QA-CHEESE-001"; name = ConvertFrom-Utf8Base64 "0KHRi9GAINCy0LXRgdC+0LLQvtC5"; barcode = "4870000000035"; uom = "Kg"; main = [decimal]"12.500"; reserve = [decimal]"1.250" },
    @{ code = "QA-ZERO-001"; name = ConvertFrom-Utf8Base64 "0KLQvtCy0LDRgCDQsdC10Lcg0L7RgdGC0LDRgtC60LA="; barcode = "4870000000042"; uom = "Nos"; main = [decimal]"0.000"; reserve = [decimal]"0.000" }
)

foreach ($item in $items) {
    Ensure-Item -BaseUrl $baseUrl -Item $item -ItemGroup $itemGroup -Session $session
    Ensure-StockQuantity `
        -BaseUrl $baseUrl `
        -ItemCode $item.code `
        -WarehouseId $MainWarehouse `
        -TargetQuantity $item.main `
        -Session $session
    Ensure-StockQuantity `
        -BaseUrl $baseUrl `
        -ItemCode $item.code `
        -WarehouseId $ReserveWarehouse `
        -TargetQuantity $item.reserve `
        -Session $session
}

$customerName = "MyRetail QA Customer"
Ensure-Customer -BaseUrl $baseUrl -CustomerName $customerName -Session $session
Ensure-MilkReservation -BaseUrl $baseUrl -CustomerName $customerName -Session $session

Write-Output "Stock QA data ready: warehouses='$MainWarehouse','$ReserveWarehouse'; reservation=$ReservationMarker"

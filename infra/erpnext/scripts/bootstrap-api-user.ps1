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
        [Parameter(Mandatory)][ValidateSet("Get", "Post", "Put")][string]$Method,
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
$existingApiEnv = @{}
if (Test-Path -LiteralPath $apiEnvPath) {
    $existingApiEnv = Read-DotEnv -Path $apiEnvPath
}

foreach ($required in @("SITE_NAME", "HTTP_PORT", "ADMIN_PASSWORD")) {
    if (-not $erpEnv.ContainsKey($required) -or -not $erpEnv[$required]) {
        throw "Required variable $required is missing from $ErpEnvFile"
    }
}

$baseUrl = "http://$($erpEnv.SITE_NAME):$($erpEnv.HTTP_PORT)"
$serviceUser = "myretail-api@local.test"
$serviceRole = "MyRetail API Reader"
$myRetailAdminRole = "MyRetail Admin"
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession

Invoke-RestMethod `
    -Method Post `
    -Uri "$baseUrl/api/method/login" `
    -WebSession $session `
    -ContentType "application/x-www-form-urlencoded" `
    -Body @{ usr = "Administrator"; pwd = $erpEnv.ADMIN_PASSWORD } | Out-Null

function Ensure-ErpRole {
    param(
        [Parameter(Mandatory)][string]$RoleName,
        [Parameter(Mandatory)][int]$DeskAccess
    )

    $encodedRole = [Uri]::EscapeDataString($RoleName)
    $roleBody = @{
        role_name = $RoleName
        desk_access = $DeskAccess
    }
    try {
        Invoke-ErpRequest -Method Get -Uri "$baseUrl/api/resource/Role/$encodedRole" -Session $session | Out-Null
        Invoke-ErpRequest -Method Put -Uri "$baseUrl/api/resource/Role/$encodedRole" -Session $session -Body $roleBody | Out-Null
    }
    catch {
        if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw }
        Invoke-ErpRequest -Method Post -Uri "$baseUrl/api/resource/Role" -Session $session -Body $roleBody | Out-Null
    }
}

Ensure-ErpRole -RoleName $serviceRole -DeskAccess 0
Ensure-ErpRole -RoleName $myRetailAdminRole -DeskAccess 0

$permissionFields = [Uri]::EscapeDataString('["name"]')
$permissionDefinitions = @(
    @{
        parent = "Supplier"
        read = 1
        select = 1
        create = 1
        write = 1
    },
    @{
        parent = "Warehouse"
        read = 1
        select = 1
    },
    @{
        parent = "Company"
        read = 1
        select = 1
    },
    @{
        parent = "Customer"
        read = 1
        select = 1
    },
    @{
        parent = "Customer Group"
        read = 1
        select = 1
    },
    @{
        parent = "Territory"
        read = 1
        select = 1
    },
    @{
        parent = "Item"
        read = 1
        select = 1
        create = 1
        write = 1
    },
    @{
        parent = "Item Barcode"
        read = 1
        select = 1
        create = 1
        write = 1
        delete = 1
    },
    @{
        parent = "Item Price"
        read = 1
        select = 1
        create = 1
        write = 1
        delete = 1
    },
    @{
        parent = "Bin"
        read = 1
        select = 1
    },
    @{
        parent = "Account"
        read = 1
        select = 1
    },
    @{
        parent = "Cost Center"
        read = 1
        select = 1
    },
    @{
        parent = "Mode of Payment"
        read = 1
        select = 1
    },
    @{
        parent = "Price List"
        read = 1
        select = 1
    },
    @{
        parent = "UOM"
        read = 1
        select = 1
    },
    @{
        parent = "POS Profile"
        read = 1
        select = 1
    },
    @{
        parent = "POS Profile User"
        read = 1
        select = 1
    },
    @{
        parent = "POS Payment Method"
        read = 1
        select = 1
    },
    @{
        parent = "POS Opening Entry"
        read = 1
        select = 1
        create = 1
        write = 1
        submit = 1
        cancel = 1
    },
    @{
        parent = "POS Opening Entry Detail"
        read = 1
        select = 1
        create = 1
        write = 1
    },
    @{
        parent = "POS Closing Entry"
        read = 1
        select = 1
        create = 1
        write = 1
        submit = 1
        cancel = 1
    },
    @{
        parent = "POS Closing Entry Detail"
        read = 1
        select = 1
        create = 1
        write = 1
    },
    @{
        parent = "POS Closing Entry Taxes"
        read = 1
        select = 1
        create = 1
        write = 1
    },
    @{
        parent = "Sales Invoice"
        read = 1
        select = 1
        create = 1
        write = 1
        submit = 1
        cancel = 1
    },
    @{
        parent = "Sales Invoice Item"
        read = 1
        select = 1
        create = 1
        write = 1
    },
    @{
        parent = "Sales Invoice Payment"
        read = 1
        select = 1
        create = 1
        write = 1
    },
    @{
        parent = "Sales Invoice Reference"
        read = 1
        select = 1
        create = 1
        write = 1
    },
    @{
        parent = "Stock Entry"
        read = 1
        select = 1
        create = 1
        write = 1
        submit = 1
    },
    @{
        parent = "Stock Entry Detail"
        read = 1
        select = 1
        create = 1
        write = 1
    },
    @{
        parent = "Purchase Receipt"
        read = 1
        select = 1
        create = 1
        write = 1
        submit = 1
        cancel = 1
    },
    @{
        parent = "Purchase Receipt Item"
        read = 1
        select = 1
        create = 1
        write = 1
    },
    @{
        parent = "Comment"
        read = 1
        select = 1
        create = 1
    }
)

foreach ($definition in $permissionDefinitions) {
    $permissionFilters = [Uri]::EscapeDataString((@{
        parent = $definition.parent
        role = $serviceRole
        permlevel = 0
    } | ConvertTo-Json -Compress))
    $permissions = Invoke-ErpRequest `
        -Method Get `
        -Uri "$baseUrl/api/resource/Custom%20DocPerm?filters=$permissionFilters&fields=$permissionFields&limit_page_length=1" `
        -Session $session

    $permissionBody = @{
        parent = $definition.parent
        role = $serviceRole
        permlevel = 0
        read = $definition.read
        select = $definition.select
        create = 0
        write = 0
        delete = 0
    }
    foreach ($permissionFlag in @("create", "write", "delete", "submit", "cancel")) {
        if ($definition.ContainsKey($permissionFlag)) {
            $permissionBody[$permissionFlag] = $definition[$permissionFlag]
        }
    }

    if (-not $permissions.data -or $permissions.data.Count -eq 0) {
        Invoke-ErpRequest `
            -Method Post `
            -Uri "$baseUrl/api/resource/Custom%20DocPerm" `
            -Session $session `
            -Body $permissionBody | Out-Null
        continue
    }

    $encodedPermission = [Uri]::EscapeDataString($permissions.data[0].name)
    Invoke-ErpRequest `
        -Method Put `
        -Uri "$baseUrl/api/resource/Custom%20DocPerm/$encodedPermission" `
        -Session $session `
        -Body $permissionBody | Out-Null
}

$customFieldDefinitions = @(
    @{ dt = "Stock Entry"; fieldname = "myretail_stock_idempotency_key"; label = "MyRetail Stock Idempotency Key"; unique = 1 },
    @{ dt = "Purchase Receipt"; fieldname = "myretail_purchase_idempotency_key"; label = "MyRetail Purchase Idempotency Key"; unique = 1 },
    @{ dt = "Sales Invoice"; fieldname = "myretail_tenant"; label = "MyRetail Tenant"; unique = 0 },
    @{ dt = "Sales Invoice"; fieldname = "myretail_sale_id"; label = "MyRetail Sale ID"; unique = 1 },
    @{ dt = "Sales Invoice"; fieldname = "myretail_sale_idempotency_key"; label = "MyRetail Sale Idempotency Key"; unique = 1 },
    @{ dt = "Sales Invoice"; fieldname = "myretail_shift_id"; label = "MyRetail Shift ID"; unique = 0 },
    @{ dt = "Sales Invoice"; fieldname = "myretail_register_id"; label = "MyRetail Register ID"; unique = 0 },
    @{ dt = "Sales Invoice"; fieldname = "myretail_cashier_email"; label = "MyRetail Cashier Email"; unique = 0 },
    @{ dt = "Sales Invoice"; fieldname = "myretail_return_id"; label = "MyRetail Return ID"; unique = 1 },
    @{ dt = "Sales Invoice"; fieldname = "myretail_return_idempotency_key"; label = "MyRetail Return Idempotency Key"; unique = 1 },
    @{ dt = "Sales Invoice"; fieldname = "myretail_return_sale_id"; label = "MyRetail Return Sale ID"; unique = 0 },
    @{ dt = "Sales Invoice"; fieldname = "myretail_return_shift_id"; label = "MyRetail Return Shift ID"; unique = 0 },
    @{ dt = "Sales Invoice"; fieldname = "myretail_return_register_id"; label = "MyRetail Return Register ID"; unique = 0 },
    @{ dt = "Sales Invoice"; fieldname = "myretail_return_cashier_email"; label = "MyRetail Return Cashier Email"; unique = 0 },
    @{ dt = "Sales Invoice"; fieldname = "myretail_return_reason"; label = "MyRetail Return Reason"; unique = 0 },
    @{ dt = "POS Opening Entry"; fieldname = "myretail_tenant"; label = "MyRetail Tenant"; unique = 0 },
    @{ dt = "POS Opening Entry"; fieldname = "myretail_shift_id"; label = "MyRetail Shift ID"; unique = 1 },
    @{ dt = "POS Opening Entry"; fieldname = "myretail_register_id"; label = "MyRetail Register ID"; unique = 0 },
    @{ dt = "POS Opening Entry"; fieldname = "myretail_cashier_email"; label = "MyRetail Cashier Email"; unique = 0 },
    @{ dt = "POS Opening Entry"; fieldname = "myretail_open_idempotency_key"; label = "MyRetail Open Idempotency Key"; unique = 1 },
    @{ dt = "POS Closing Entry"; fieldname = "myretail_tenant"; label = "MyRetail Tenant"; unique = 0 },
    @{ dt = "POS Closing Entry"; fieldname = "myretail_shift_id"; label = "MyRetail Shift ID"; unique = 0 },
    @{ dt = "POS Closing Entry"; fieldname = "myretail_close_idempotency_key"; label = "MyRetail Close Idempotency Key"; unique = 1 },
    @{ dt = "POS Closing Entry"; fieldname = "myretail_register_id"; label = "MyRetail Register ID"; unique = 0 },
    @{ dt = "POS Closing Entry"; fieldname = "myretail_cashier_email"; label = "MyRetail Cashier Email"; unique = 0 }
)

foreach ($definition in $customFieldDefinitions) {
    $fieldName = "$($definition.dt)-$($definition.fieldname)"
    $encodedFieldName = [Uri]::EscapeDataString($fieldName)
    $fieldBody = @{
        dt = $definition.dt
        fieldname = $definition.fieldname
        label = $definition.label
        fieldtype = "Data"
        insert_after = "remarks"
        unique = $definition.unique
        no_copy = 1
        allow_on_submit = 1
    }

    try {
        Invoke-ErpRequest `
            -Method Get `
            -Uri "$baseUrl/api/resource/Custom%20Field/$encodedFieldName" `
            -Session $session | Out-Null
        Invoke-ErpRequest `
            -Method Put `
            -Uri "$baseUrl/api/resource/Custom%20Field/$encodedFieldName" `
            -Session $session `
            -Body $fieldBody | Out-Null
    }
    catch {
        if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw }
        $fieldBody["doctype"] = "Custom Field"
        Invoke-ErpRequest `
            -Method Post `
            -Uri "$baseUrl/api/resource/Custom%20Field" `
            -Session $session `
            -Body $fieldBody | Out-Null
    }
}

function Ensure-ErpUser {
    param(
        [Parameter(Mandatory)][string]$Email,
        [Parameter(Mandatory)][string]$FirstName
    )

    $encodedUser = [Uri]::EscapeDataString($Email)
    try {
        Invoke-ErpRequest -Method Get -Uri "$baseUrl/api/resource/User/$encodedUser" -Session $session | Out-Null
    }
    catch {
        if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw }
        Invoke-ErpRequest -Method Post -Uri "$baseUrl/api/resource/User" -Session $session -Body @{
            email = $Email
            first_name = $FirstName
            enabled = 1
            user_type = "System User"
            send_welcome_email = 0
            roles = @(@{ role = $serviceRole })
        } | Out-Null
    }
}

function Get-ProfileUserEmail {
    param([Parameter(Mandatory)][string]$ProfileName)

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($ProfileName)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha256.ComputeHash($bytes)
    }
    finally {
        $sha256.Dispose()
    }
    $hash = -join ($hashBytes[0..5] | ForEach-Object { $_.ToString("x2") })
    return "myretail-pos-$hash@local.test"
}

Ensure-ErpUser -Email $serviceUser -FirstName "MyRetail API"

$posProfileNames = @()
try {
    $posProfileFields = [Uri]::EscapeDataString('["name"]')
    $posProfileFilters = [Uri]::EscapeDataString('[["POS Profile","disabled","=",0]]')
    $profiles = Invoke-ErpRequest `
        -Method Get `
        -Uri "$baseUrl/api/resource/POS%20Profile?fields=$posProfileFields&filters=$posProfileFilters&limit_page_length=100" `
        -Session $session
    foreach ($profile in $profiles.data) {
        if ($profile.name) {
            $posProfileNames += $profile.name
        }
    }
}
catch {
    $posProfileNames = @()
}
$posUserMap = @{}
$posCredentialMap = @{}
for ($index = 0; $index -lt $posProfileNames.Count; $index++) {
    $posUser = Get-ProfileUserEmail -ProfileName $posProfileNames[$index]
    Ensure-ErpUser -Email $posUser -FirstName "MyRetail POS $($index + 1)"
    $posUserMap[$posProfileNames[$index]] = $posUser

    $posKeys = Invoke-RestMethod `
        -Method Post `
        -Uri "$baseUrl/api/method/frappe.core.doctype.user.user.generate_keys" `
        -WebSession $session `
        -ContentType "application/x-www-form-urlencoded" `
        -Body @{ user = $posUser }
    if (-not $posKeys.message.api_key -or -not $posKeys.message.api_secret) {
        throw "ERPNext did not return POS user API keys"
    }
    $posCredentialMap[$posProfileNames[$index]] = "$($posKeys.message.api_key):$($posKeys.message.api_secret)"

    $encodedProfileName = [Uri]::EscapeDataString($posProfileNames[$index])
    $profile = Invoke-ErpRequest `
        -Method Get `
        -Uri "$baseUrl/api/resource/POS%20Profile/$encodedProfileName" `
        -Session $session
    $profileUsers = @()
    if ($profile.data.applicable_for_users) {
        foreach ($profileUser in $profile.data.applicable_for_users) {
            if ($profileUser.user -and $profileUser.user -ne $posUser) {
                $profileUsers += @{ user = $profileUser.user; default = $profileUser.default }
            }
        }
    }
    $profileUsers += @{ user = $posUser; default = 0 }
    Invoke-ErpRequest `
        -Method Put `
        -Uri "$baseUrl/api/resource/POS%20Profile/$encodedProfileName" `
        -Session $session `
        -Body @{ applicable_for_users = $profileUsers } | Out-Null
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

$authSecret = $existingApiEnv["MYRETAIL_AUTH_SECRET"]
if (-not $authSecret) {
    $secretBytes = New-Object byte[] 32
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($secretBytes)
    }
    finally {
        $random.Dispose()
    }
    $authSecret = -join ($secretBytes | ForEach-Object { $_.ToString("x2") })
}

function Get-ApiSetting {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Default
    )

    if ($existingApiEnv.ContainsKey($Name) -and $existingApiEnv[$Name]) {
        return $existingApiEnv[$Name]
    }
    return $Default
}

$apiEnv = @(
    "MYRETAIL_ENVIRONMENT=$(Get-ApiSetting -Name 'MYRETAIL_ENVIRONMENT' -Default 'development')"
    "MYRETAIL_LOG_LEVEL=$(Get-ApiSetting -Name 'MYRETAIL_LOG_LEVEL' -Default 'INFO')"
    "MYRETAIL_TENANT_SLUG=$(Get-ApiSetting -Name 'MYRETAIL_TENANT_SLUG' -Default 'myretail')"
    "MYRETAIL_AUTH_SECRET=$authSecret"
    "MYRETAIL_AUTH_TOKEN_TTL_SECONDS=$(Get-ApiSetting -Name 'MYRETAIL_AUTH_TOKEN_TTL_SECONDS' -Default '3600')"
    "MYRETAIL_AUTH_RATE_LIMIT_ATTEMPTS=$(Get-ApiSetting -Name 'MYRETAIL_AUTH_RATE_LIMIT_ATTEMPTS' -Default '5')"
    "MYRETAIL_AUTH_RATE_LIMIT_WINDOW_SECONDS=$(Get-ApiSetting -Name 'MYRETAIL_AUTH_RATE_LIMIT_WINDOW_SECONDS' -Default '300')"
    "MYRETAIL_ERPNEXT_BASE_URL=$baseUrl"
    "MYRETAIL_ERPNEXT_API_KEY=$($keys.message.api_key)"
    "MYRETAIL_ERPNEXT_API_SECRET=$($keys.message.api_secret)"
    "MYRETAIL_ERPNEXT_TIMEOUT_SECONDS=$(Get-ApiSetting -Name 'MYRETAIL_ERPNEXT_TIMEOUT_SECONDS' -Default '10')"
    "MYRETAIL_ERPNEXT_SELLING_PRICE_LIST=$(Get-ApiSetting -Name 'MYRETAIL_ERPNEXT_SELLING_PRICE_LIST' -Default 'Standard Selling')"
    "MYRETAIL_ERPNEXT_BUYING_PRICE_LIST=$(Get-ApiSetting -Name 'MYRETAIL_ERPNEXT_BUYING_PRICE_LIST' -Default 'Standard Buying')"
    "MYRETAIL_ERPNEXT_COMPANY=$(Get-ApiSetting -Name 'MYRETAIL_ERPNEXT_COMPANY' -Default 'MyRetail Demo')"
    "MYRETAIL_ERPNEXT_API_USER=$serviceUser"
    "MYRETAIL_ERPNEXT_POS_USER=$(Get-ApiSetting -Name 'MYRETAIL_ERPNEXT_POS_USER' -Default $serviceUser)"
    "MYRETAIL_ERPNEXT_POS_USER_MAP=$($posUserMap | ConvertTo-Json -Compress)"
    "MYRETAIL_ERPNEXT_POS_CREDENTIALS_MAP=$($posCredentialMap | ConvertTo-Json -Compress)"
    "MYRETAIL_POS_CASHIER_ASSIGNMENTS=$(Get-ApiSetting -Name 'MYRETAIL_POS_CASHIER_ASSIGNMENTS' -Default '{}')"
    "MYRETAIL_DEFAULT_CURRENCY=$(Get-ApiSetting -Name 'MYRETAIL_DEFAULT_CURRENCY' -Default 'KZT')"
) -join [Environment]::NewLine

$apiEnvDirectory = Split-Path -Parent $apiEnvPath
[System.IO.Directory]::CreateDirectory($apiEnvDirectory) | Out-Null
[System.IO.File]::WriteAllText($apiEnvPath, $apiEnv + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))

Write-Output "ERPNext service user configured. Secrets were written only to $ApiEnvFile."

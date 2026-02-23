param(
    [string]$SubscriptionId,
    [string]$ResourceGroup = "dns-zones",
    [string]$Name = "dns-updater-sp",
    [string]$DnsMxTarget,
    [string]$DnsCnameTarget
)

$root = $PSScriptRoot
$envPath = Join-Path $root ".env"

if (Test-Path -LiteralPath $envPath) {
    Write-Host ".env already exists at $envPath. Skipping init." -ForegroundColor Yellow
    exit 0
}

if (-not $SubscriptionId) {
    $SubscriptionId = Read-Host "Enter Azure Subscription ID"
}
if (-not $DnsMxTarget) {
    $DnsMxTarget = Read-Host "Enter DNS_MX_TARGET (MX exchange)"
}
if (-not $DnsCnameTarget) {
    $DnsCnameTarget = Read-Host "Enter DNS_CNAME_TARGET (CNAME target)"
}

$spScript = Join-Path $root "src\create-dns-sp.ps1"
if (-not (Test-Path -LiteralPath $spScript)) {
    throw "create-dns-sp.ps1 not found at $spScript"
}

Write-Host "Creating service principal..." -ForegroundColor Cyan
$sp = & $spScript -SubscriptionId $SubscriptionId -ResourceGroup $ResourceGroup -Name $Name

if (-not $sp) {
    throw "Service principal creation failed."
}

@"
AZURE_TENANT_ID=$($sp.tenant)
AZURE_CLIENT_ID=$($sp.appId)
AZURE_CLIENT_SECRET=$($sp.password)
AZURE_SUBSCRIPTION_ID=$SubscriptionId

AZURE_RESOURCE_GROUP=$ResourceGroup
DNS_MX_TARGET=$DnsMxTarget
DNS_CNAME_TARGET=$DnsCnameTarget

# Optional
# DNS_A_TARGET=
# IP_STATE_FILE=/state/last_ip.txt
# INTERVAL_SECONDS=300
# TTL=3600
# MX_PREFERENCE=10
# FORCE_MX=true
# FORCE_CNAME=true
"@ | Set-Content -LiteralPath $envPath -Encoding utf8 -Force

Write-Host "Created .env at $envPath" -ForegroundColor Green
Write-Host "You can now run docker compose up --build" -ForegroundColor Green

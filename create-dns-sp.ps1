param(
    [string]$SubscriptionId,
    [string]$ResourceGroup = "dns-zones",
    [string]$Name = "dns-updater-sp"
)

if (-not $SubscriptionId) {
    throw "You must provide -SubscriptionId"
}

Write-Host "Setting subscription..."
az account set --subscription $SubscriptionId

Write-Host "Creating service principal scoped to resource group '$ResourceGroup'..."

$scope = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup"

$sp = az ad sp create-for-rbac `
    --name $Name `
    --role "DNS Zone Contributor" `
    --scopes $scope `
    --output json | ConvertFrom-Json

if (-not $sp) {
    throw "Failed to create service principal."
}

Write-Host ""
Write-Host "==== SERVICE PRINCIPAL CREATED ====" -ForegroundColor Green
Write-Host ""
Write-Host "Tenant ID:       $($sp.tenant)"
Write-Host "Client ID:       $($sp.appId)"
Write-Host "Client Secret:   $($sp.password)"
Write-Host "Subscription ID: $SubscriptionId"
Write-Host ""
Write-Host "Use these as environment variables in Docker:"
Write-Host ""
Write-Host "AZURE_TENANT_ID=$($sp.tenant)"
Write-Host "AZURE_CLIENT_ID=$($sp.appId)"
Write-Host "AZURE_CLIENT_SECRET=$($sp.password)"
Write-Host "AZURE_SUBSCRIPTION_ID=$SubscriptionId"
Write-Host ""
Write-Host "Store the secret securely. It will not be shown again."
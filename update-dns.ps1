<#
Azure DNS updater (env-only, production-safe)

REQUIRED ENV VARS (Service Principal auth):
  AZURE_TENANT_ID
  AZURE_CLIENT_ID
  AZURE_SUBSCRIPTION_ID
  AZURE_CLIENT_SECRET        OR  AZURE_CLIENT_SECRET_FILE (Docker secret path)

REQUIRED DNS ENV VARS:
  AZURE_RESOURCE_GROUP
  DNS_MX_TARGET
  DNS_CNAME_TARGET

OPTIONAL DNS ENV VARS:
  DNS_A_TARGET               (static IPv4; if set, disables dynamic IP lookup)

OPTIONAL BEHAVIOR ENV VARS:
  RUN_ONCE                   ("true"/"1") -> run single iteration then exit
  INTERVAL_SECONDS           (default: 300)
  TTL                        (default: 3600)
  MX_PREFERENCE              (default: 10)
  FORCE_MX                   ("true"/"1") -> overwrite MX even if exists
  FORCE_CNAME                ("true"/"1") -> overwrite CNAME even if exists
  BLACKLIST_ZONES            comma-separated list of zone names to skip
  IP_SERVICES                comma-separated list of public IP lookup URLs
  IP_STATE_FILE              optional path to persist last desired IP (helps fast-path; safe if missing)

A(@) policy:
- Always enforce A(@) == desired IPv4:
  - If DNS_A_TARGET set: desired = DNS_A_TARGET
  - Else desired = detected public IP
- If A(@) missing / has multiple A records / differs -> replace with exactly one A record = desired

State file behavior:
- If IP_STATE_FILE set + readable:
  - When desired IP changes vs stored -> fast-path update A(@) in all zones (no reads)
  - When desired IP same as stored -> per-zone check and update only if drifted
- If IP_STATE_FILE not set: per-zone check every time (works fine)
#>

# -----------------------------
# Logging
# -----------------------------
function TS { (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") }
function Log([string]$Level, [string]$Msg) { Write-Host "[$(TS)] [$Level] $Msg" }
function Fail([string]$Msg) { throw "[$(TS)] [ERROR] $Msg" }

# -----------------------------
# Env helpers
# -----------------------------
function Get-Env([string]$Name) { [Environment]::GetEnvironmentVariable($Name) }

function Get-Bool([string]$Name) {
    $v = Get-Env $Name
    if ([string]::IsNullOrWhiteSpace($v)) { return $false }
    return $v.ToLowerInvariant() -in @("1","true","yes","y")
}

function Get-Secret {
    $direct = Get-Env "AZURE_CLIENT_SECRET"
    if (-not [string]::IsNullOrWhiteSpace($direct)) { return $direct }

    $file = Get-Env "AZURE_CLIENT_SECRET_FILE"
    if (-not [string]::IsNullOrWhiteSpace($file) -and (Test-Path -LiteralPath $file)) {
        return (Get-Content -LiteralPath $file -ErrorAction Stop | Select-Object -First 1).ToString().Trim()
    }
    return $null
}

function Is-IPv4([string]$ip) {
    return ($ip -match '^(?:\d{1,3}\.){3}\d{1,3}$')
}

# -----------------------------
# Read configuration
# -----------------------------
$TenantId     = Get-Env "AZURE_TENANT_ID"
$ClientId     = Get-Env "AZURE_CLIENT_ID"
$Subscription = Get-Env "AZURE_SUBSCRIPTION_ID"
$ClientSecret = Get-Secret

$ResourceGroup = Get-Env "AZURE_RESOURCE_GROUP"

$DnsMxTarget    = Get-Env "DNS_MX_TARGET"
$DnsCnameTarget = Get-Env "DNS_CNAME_TARGET"
$DnsATarget     = Get-Env "DNS_A_TARGET"   # optional

$RunOnce        = Get-Bool "RUN_ONCE"
$ForceMx        = Get-Bool "FORCE_MX"
$ForceCname     = Get-Bool "FORCE_CNAME"

$IntervalSeconds = [int](Get-Env "INTERVAL_SECONDS"); if ($IntervalSeconds -le 0) { $IntervalSeconds = 300 }
$Ttl            = [int](Get-Env "TTL");              if ($Ttl -le 0) { $Ttl = 3600 }
$MxPreference   = [int](Get-Env "MX_PREFERENCE");    if ($MxPreference -lt 0) { $MxPreference = 10 }

$BlacklistZones = @()
$bl = Get-Env "BLACKLIST_ZONES"
if (-not [string]::IsNullOrWhiteSpace($bl)) {
    $BlacklistZones = $bl.Split(",") | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ }
}

$IpServices = @(
    "https://api.ipify.org?format=text",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip"
)
$customIpServices = Get-Env "IP_SERVICES"
if (-not [string]::IsNullOrWhiteSpace($customIpServices)) {
    $IpServices = $customIpServices.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ }
}

$IpStateFile = Get-Env "IP_STATE_FILE"  # optional (may be empty)

# Validate required config (no defaults here)
$required = @{
    AZURE_TENANT_ID        = $TenantId
    AZURE_CLIENT_ID        = $ClientId
    AZURE_SUBSCRIPTION_ID  = $Subscription
    AZURE_CLIENT_SECRET    = $ClientSecret
    AZURE_RESOURCE_GROUP   = $ResourceGroup
    DNS_MX_TARGET          = $DnsMxTarget
    DNS_CNAME_TARGET       = $DnsCnameTarget
}
$missing = $required.GetEnumerator() | Where-Object { [string]::IsNullOrWhiteSpace($_.Value) }
if ($missing.Count -gt 0) {
    $names = ($missing | ForEach-Object { $_.Key }) -join ", "
    Fail "Missing required environment variables: $names"
}

if (-not [string]::IsNullOrWhiteSpace($DnsATarget) -and -not (Is-IPv4 $DnsATarget.Trim())) {
    Fail "DNS_A_TARGET is set but is not a valid IPv4 address: '$DnsATarget'"
}

# -----------------------------
# Azure Login
# -----------------------------
function Az-AssertOk([string]$Context) {
    if ($LASTEXITCODE -ne 0) { Fail "$Context (az exit code $LASTEXITCODE)" }
}

Log "INFO" "Authenticating to Azure with Service Principal..."
az login --service-principal --username $ClientId --password $ClientSecret --tenant $TenantId | Out-Null
Az-AssertOk "Azure login failed"

az account set --subscription $Subscription | Out-Null
Az-AssertOk "Failed to set Azure subscription"

Log "INFO" "Azure auth OK. RG='$ResourceGroup' TTL=$Ttl MXPref=$MxPreference"

$aSource = "dynamic (public IP lookup)"
if (-not [string]::IsNullOrWhiteSpace($DnsATarget)) {
    $aSource = "static (DNS_A_TARGET)"
}

$stateDesc = "(disabled)"
if (-not [string]::IsNullOrWhiteSpace($IpStateFile)) {
    $stateDesc = $IpStateFile
}

Log "INFO" ("A source: " + $aSource)
Log "INFO" ("State file: " + $stateDesc)

# -----------------------------
# Public IP / Desired IP
# -----------------------------
function Get-PublicIp {
    foreach ($url in $IpServices) {
        try {
            $ip = (Invoke-RestMethod -Uri $url -TimeoutSec 10 -ErrorAction Stop).ToString().Trim()
            if (Is-IPv4 $ip) { return $ip }
        } catch { }
    }
    Fail "Public IP lookup failed (all services failed)."
}

function Ensure-StateDir([string]$Path) {
    $dir = Split-Path -Parent $Path
    if ([string]::IsNullOrWhiteSpace($dir)) { return }
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}

function Read-LastDesiredIp([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    try {
        if (Test-Path -LiteralPath $Path) {
            $v = (Get-Content -LiteralPath $Path -ErrorAction Stop | Select-Object -First 1).ToString().Trim()
            if (Is-IPv4 $v) { return $v }
        }
    } catch { }
    return $null
}

function Write-LastDesiredIp([string]$Path, [string]$Ip) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    Ensure-StateDir -Path $Path
    Set-Content -LiteralPath $Path -Value $Ip -Force
}

# -----------------------------
# Azure DNS operations
# -----------------------------
function Get-AllZones([string]$Rg) {
    $zones = az network dns zone list -g $Rg --query "[].name" -o tsv 2>$null
    Az-AssertOk "Failed to list DNS zones in resource group '$Rg'"
    return @($zones | Where-Object { $_ -and $_.Trim() })
}

function RecordSet-Exists([string]$Zone, [string]$Name, [string]$Type, [string]$Rg) {
    az network dns record-set $Type show -g $Rg -z $Zone -n $Name -o none 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Get-ZoneRootAIPs([string]$Zone, [string]$Rg) {
    $json = az network dns record-set a show -g $Rg -z $Zone -n "@" -o json 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($json)) { return @() }
    try {
        $obj = $json | ConvertFrom-Json
        return @($obj.arecords | ForEach-Object { $_.ipv4Address }) | Where-Object { $_ }
    } catch {
        return @()
    }
}

function Replace-ZoneRootA([string]$Zone, [string]$DesiredIp, [string]$Rg, [int]$TtlX) {
    # Replace with exactly one A record = DesiredIp
    az network dns record-set a delete -g $Rg -z $Zone -n "@" --yes 2>$null | Out-Null
    az network dns record-set a create -g $Rg -z $Zone -n "@" --ttl $TtlX | Out-Null
    az network dns record-set a add-record -g $Rg -z $Zone -n "@" -a $DesiredIp | Out-Null
    Az-AssertOk "Failed updating A(@) for zone '$Zone'"
}

function Ensure-MX([string]$Zone, [string]$Rg) {
    $exists = RecordSet-Exists -Zone $Zone -Name "@" -Type "mx" -Rg $Rg
    $mxMatch = $false
    if ($exists -and -not $ForceMx) {
        # Check if MX record matches target
        $json = az network dns record-set mx show -g $Rg -z $Zone -n "@" -o json 2>$null
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($json)) {
            try {
                $obj = $json | ConvertFrom-Json
                if ($obj.mxRecords.Count -eq 1) {
                    $mxExchange = $obj.mxRecords[0].exchange.ToString().Trim()
                    $mxPref = [int]$obj.mxRecords[0].preference
                    $targetExchange = $DnsMxTarget.ToString().Trim()
                    $targetPref = [int]$MxPreference
                    if ($mxExchange -eq $targetExchange -and $mxPref -eq $targetPref) {
                        $mxMatch = $true
                    } else {
                        Log "DEBUG" "MX(@) compare: existing='$mxExchange'/$mxPref desired='$targetExchange'/$targetPref"
                    }
                } else {
                    Log "DEBUG" "MX(@) record count: $($obj.mxRecords.Count) (should be 1)"
                }
            } catch {}
        }
    }
    if (-not $exists -or $ForceMx -or -not $mxMatch) {
        if ($exists -and $ForceMx) {
            Log "INFO" "  Overwriting MX(@) -> $DnsMxTarget (pref $MxPreference)"
            az network dns record-set mx delete -g $Rg -z $Zone -n "@" --yes 2>$null | Out-Null
        } elseif (-not $exists) {
            Log "INFO" "  Creating MX(@) -> $DnsMxTarget (pref $MxPreference)"
        } else {
            Log "INFO" "  Updating MX(@): existing does not match target."
        }
        az network dns record-set mx create -g $Rg -z $Zone -n "@" --ttl $Ttl | Out-Null
        az network dns record-set mx add-record -g $Rg -z $Zone -n "@" --exchange $DnsMxTarget --preference $MxPreference | Out-Null
        Az-AssertOk "Failed ensuring MX(@) for zone '$Zone'"
        return $true
    }
    Log "INFO" "  MX(@) already $DnsMxTarget (pref $MxPreference); skipping."
    return $false
}

function Ensure-CNAMEWildcard([string]$Zone, [string]$Rg) {
    $exists = RecordSet-Exists -Zone $Zone -Name "*" -Type "cname" -Rg $Rg
    $cnameMatch = $false
    if ($exists -and -not $ForceCname) {
        $json = az network dns record-set cname show -g $Rg -z $Zone -n "*" -o json 2>$null
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($json)) {
            try {
                $obj = $json | ConvertFrom-Json
                $existingCname = $obj.cname.ToString().Trim()
                $targetCname = $DnsCnameTarget.ToString().Trim()
                if ($existingCname -eq $targetCname) {
                    $cnameMatch = $true
                }
                Log "DEBUG" "CNAME(*) compare: existing='$existingCname' desired='$targetCname'"
            } catch {}
        }
    }
    if (-not $exists -or $ForceCname -or -not $cnameMatch) {
        if ($exists -and $ForceCname) {
            Log "INFO" "  Overwriting CNAME(*) for $Zone -> $DnsCnameTarget"
            az network dns record-set cname delete -g $Rg -z $Zone -n "*" --yes 2>$null | Out-Null
        } elseif (-not $exists) {
            Log "INFO" "  Creating CNAME(*) for $Zone -> $DnsCnameTarget"
        } else {
            Log "INFO" "  Updating CNAME(*) for $($Zone): existing does not match target."
        }
        az network dns record-set cname create -g $Rg -z $Zone -n "*" --ttl $Ttl | Out-Null
        az network dns record-set cname set-record -g $Rg -z $Zone -n "*" --cname $DnsCnameTarget | Out-Null
        Az-AssertOk "Failed ensuring CNAME(*) for zone '$Zone'"
        return $true
    }
    Log "INFO" "  CNAME(*) for $Zone already $DnsCnameTarget; skipping."
    return $false
}

# -----------------------------
# Main loop
# -----------------------------
function Process-Once {
    # Determine desired IP
    $desiredIp = if (-not [string]::IsNullOrWhiteSpace($DnsATarget)) { $DnsATarget.Trim() } else { Get-PublicIp }
    Log "INFO" "Desired A(@) IPv4: $desiredIp"

    # State-file based fast-path hint (optional)
    $storedDesired = Read-LastDesiredIp -Path $IpStateFile
    if ($storedDesired) { Log "INFO" "Stored desired IP: $storedDesired" }

    $canFastPath = (-not [string]::IsNullOrWhiteSpace($IpStateFile)) -and $storedDesired -and ($storedDesired -ne $desiredIp)

    if ($canFastPath) {
        Log "INFO" "Desired IP changed since last run; will fast-path update A(@) for all zones."
    } else {
        Log "INFO" "Will enforce A(@) per-zone (update only when mismatched)."
    }

    $zones = Get-AllZones -Rg $ResourceGroup
    if ($zones.Count -eq 0) {
        Log "WARN" "No zones found in resource group '$ResourceGroup'."
        return
    }

    $aUpdated = 0
    $mxChanged = 0
    $cnameChanged = 0
    $failed = 0

    # Pass 1: A(@) for all zones
    foreach ($zone in $zones) {
        $zoneLower = $zone.ToLowerInvariant()
        if ($BlacklistZones -contains $zoneLower) {
            Log "INFO" "Skipping blacklisted zone: $zone"
            continue
        }
        Log "INFO" "=== Zone: $zone ==="
        try {
            if ($canFastPath) {
                Log "INFO" "  Updating A(@) -> $desiredIp"
                Replace-ZoneRootA -Zone $zone -DesiredIp $desiredIp -Rg $ResourceGroup -TtlX $Ttl
                $aUpdated++
            } else {
                $existingRaw = Get-ZoneRootAIPs -Zone $zone -Rg $ResourceGroup
                $existing = @($existingRaw)
                $needsUpdate = $false
                $existingIp = if ($existing.Count -eq 1) { $existing[0].ToString().Trim() } else { "" }
                $desiredIpTrim = $desiredIp.ToString().Trim()
                if ($existing.Count -ne 1) {
                    $needsUpdate = $true
                } elseif ($existingIp -ne $desiredIpTrim) {
                    Log "DEBUG" "A(@) compare: existing='$existingIp' desired='$desiredIpTrim'"
                    $needsUpdate = $true
                }
                if ($needsUpdate) {
                    $existingStr = if ($existing.Count -gt 0) { ($existing -join ", ") } else { "(none)" }
                    Log "INFO" "  Updating A(@): $existingStr -> $desiredIp"
                    Replace-ZoneRootA -Zone $zone -DesiredIp $desiredIp -Rg $ResourceGroup -TtlX $Ttl
                    $aUpdated++
                } else {
                    Log "INFO" "  A(@) already $desiredIp; skipping."
                }
            }
        } catch {
            $failed++
            Log "ERROR" "Zone failed (A): $zone :: $($_.Exception.Message)"
        }
    }

    # Pass 2: CNAME for all zones
    foreach ($zone in $zones) {
        $zoneLower = $zone.ToLowerInvariant()
        if ($BlacklistZones -contains $zoneLower) { continue }
        Log "INFO" "=== Zone: $zone ==="
        try {
            if (Ensure-CNAMEWildcard -Zone $zone -Rg $ResourceGroup) { $cnameChanged++ }
        } catch {
            $failed++
            Log "ERROR" "Zone failed (CNAME): $zone :: $($_.Exception.Message)"
        }
    }

    # Pass 3: MX for all zones
    foreach ($zone in $zones) {
        $zoneLower = $zone.ToLowerInvariant()
        if ($BlacklistZones -contains $zoneLower) { continue }
        Log "INFO" "=== Zone: $zone ==="
        try {
            if (Ensure-MX -Zone $zone -Rg $ResourceGroup) { $mxChanged++ }
        } catch {
            $failed++
            Log "ERROR" "Zone failed (MX): $zone :: $($_.Exception.Message)"
        }
    }

    # Persist desired IP (if enabled)
    Write-LastDesiredIp -Path $IpStateFile -Ip $desiredIp
    if ($IpStateFile) { Log "INFO" "Wrote desired IP to state file: $IpStateFile" }

    Log "INFO" "Summary: AUpdated=$aUpdated MXChanged=$mxChanged CNAMEChanged=$cnameChanged FailedZones=$failed ZonesTotal=$($zones.Count)"
}

if ($RunOnce) {
    Log "INFO" "RUN_ONCE=true -> running single iteration."
    Process-Once
    exit 0
}

Log "INFO" "Watch loop starting. IntervalSeconds=$IntervalSeconds (set RUN_ONCE=true to disable)"
while ($true) {
    try {
        Process-Once
    } catch {
        Log "ERROR" $_.Exception.Message
    }
    $remaining = $IntervalSeconds
    while ($remaining -gt 0) {
        $logInterval = [Math]::Min(10, $remaining)
        Log "INFO" "Sleeping... $remaining seconds until next run."
        Start-Sleep -Seconds $logInterval
        $remaining -= $logInterval
    }
}
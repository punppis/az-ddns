#!/bin/bash
# Azure Dynamic DNS Updater
# Periodically checks public IP and updates Azure DNS A record
#
# Configuration: set via environment variables or /etc/az-ddns/az-ddns.conf
# Environment variables take precedence.

set -euo pipefail

# ── Defaults (overridable by env vars or config file) ──────────────────────

AZ_RESOURCE_GROUP="${AZ_RESOURCE_GROUP:-}"
AZ_DNS_ZONE="${AZ_DNS_ZONE:-changeme.example.com}"
AZ_RECORD_NAME="${AZ_RECORD_NAME:-@}"
AZ_RECORD_TTL="${AZ_RECORD_TTL:-300}"
IP_SERVICE_URL="${IP_SERVICE_URL:-https://api.ipify.org}"
IP_SERVICE_COMMAND="${IP_SERVICE_COMMAND:-}"
STATE_FILE="${STATE_FILE:-/var/lib/az-ddns/last_ip.txt}"
LOG_FILE="${LOG_FILE:-}"

# Azure CLI config directory (override for non-root container use)
AZURE_CONFIG_DIR="${AZURE_CONFIG_DIR:-${HOME}/.azure}"
export AZURE_CONFIG_DIR

# Config file overrides defaults (but env vars already set take precedence)
CONFIG_FILE="/etc/az-ddns/az-ddns.conf"
if [[ -f "$CONFIG_FILE" ]]; then
    set -a; source "$CONFIG_FILE"; set +a
fi

# Validate required settings
if [[ -z "${AZ_RESOURCE_GROUP:-}" ]]; then
    echo "ERROR: AZ_RESOURCE_GROUP is not set. Provide via environment variable or config file." >&2
    exit 1
fi

# Logging function
log_msg() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg"
    if [[ -n "${LOG_FILE:-}" ]] && [[ -w "$(dirname "$LOG_FILE")" || -w "$LOG_FILE" ]]; then
        echo "$msg" >> "$LOG_FILE"
    fi
}

# Ensure state directory exists
STATE_DIR="$(dirname "$STATE_FILE")"
mkdir -p "$STATE_DIR"

# Get current public IP
if [[ -n "${IP_SERVICE_COMMAND:-}" ]]; then
    log_msg "Running IP command: $IP_SERVICE_COMMAND"
    PUBLIC_IP=$(eval "$IP_SERVICE_COMMAND" 2>/dev/null) || true
elif [[ -n "${IP_SERVICE_URL:-}" ]]; then
    log_msg "Checking public IP from $IP_SERVICE_URL..."
    PUBLIC_IP=$(curl -sS --connect-timeout 10 --max-time 15 --tlsv1.2 --proto =https "$IP_SERVICE_URL" 2>/dev/null)
else
    log_msg "ERROR: Neither IP_SERVICE_COMMAND nor IP_SERVICE_URL is configured."
    exit 1
fi

# Validate it looks like an IPv4 address
if ! [[ "$PUBLIC_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    log_msg "ERROR: Failed to get valid public IP. Got: '$PUBLIC_IP'"
    exit 1
fi

log_msg "Public IP: $PUBLIC_IP"

# Check last known IP
LAST_IP=""
if [[ -f "$STATE_FILE" ]]; then
    LAST_IP=$(cat "$STATE_FILE")
fi

if [[ "$PUBLIC_IP" == "$LAST_IP" ]]; then
    log_msg "IP unchanged ($PUBLIC_IP). No update needed."
    exit 0
fi

# Get current Azure DNS record value
log_msg "Checking current Azure DNS A record for ${AZ_RECORD_NAME}.${AZ_DNS_ZONE}..."
CURRENT_AZ_IP=$(az network dns record-set a show \
    --resource-group "$AZ_RESOURCE_GROUP" \
    --zone-name "$AZ_DNS_ZONE" \
    --name "$AZ_RECORD_NAME" \
    --query "ARecords[0].ipv4Address" \
    --output tsv 2>/dev/null || echo "")

if [[ "$PUBLIC_IP" == "$CURRENT_AZ_IP" ]]; then
    log_msg "Azure DNS already set to $PUBLIC_IP. Updating state file only."
    echo "$PUBLIC_IP" > "$STATE_FILE"
    exit 0
fi

# Update or create the A record
if [[ -n "$CURRENT_AZ_IP" ]]; then
    # Record exists — update it
    log_msg "Updating Azure DNS: ${AZ_RECORD_NAME}.${AZ_DNS_ZONE} -> $PUBLIC_IP (TTL=${AZ_RECORD_TTL})..."
    if az network dns record-set a update \
        --resource-group "$AZ_RESOURCE_GROUP" \
        --zone-name "$AZ_DNS_ZONE" \
        --name "$AZ_RECORD_NAME" \
        --set "arecords[0].ipv4Address=$PUBLIC_IP" \
        --set "ttl=$AZ_RECORD_TTL" 2>&1; then
        log_msg "SUCCESS: DNS updated to $PUBLIC_IP"
        echo "$PUBLIC_IP" > "$STATE_FILE"
    else
        log_msg "ERROR: Failed to update DNS record"
        exit 1
    fi
else
    # Record does not exist — create it
    log_msg "Creating Azure DNS A record: ${AZ_RECORD_NAME}.${AZ_DNS_ZONE} -> $PUBLIC_IP (TTL=${AZ_RECORD_TTL})..."
    if az network dns record-set a create \
        --resource-group "$AZ_RESOURCE_GROUP" \
        --zone-name "$AZ_DNS_ZONE" \
        --name "$AZ_RECORD_NAME" \
        --ttl "$AZ_RECORD_TTL" 2>&1 && \
       az network dns record-set a add-record \
        --resource-group "$AZ_RESOURCE_GROUP" \
        --zone-name "$AZ_DNS_ZONE" \
        --record-set-name "$AZ_RECORD_NAME" \
        --ipv4-address "$PUBLIC_IP" 2>&1 && \
       az network dns record-set a update \
        --resource-group "$AZ_RESOURCE_GROUP" \
        --zone-name "$AZ_DNS_ZONE" \
        --name "$AZ_RECORD_NAME" \
        --set "ttl=$AZ_RECORD_TTL" 2>&1; then
        log_msg "SUCCESS: DNS record created with $PUBLIC_IP"
        echo "$PUBLIC_IP" > "$STATE_FILE"
    else
        log_msg "ERROR: Failed to create DNS record"
        exit 1
    fi
fi

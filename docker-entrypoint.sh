#!/bin/bash
# Docker entrypoint for Azure DDNS
# Runs the DDNS update script on a configurable interval with graceful shutdown.

set -euo pipefail

INTERVAL="${INTERVAL:-300}"
SCRIPT="/usr/local/bin/az-ddns.sh"

echo "=== Azure DDNS Container ==="
echo "Interval: ${INTERVAL}s"
echo "DNS Zone: ${AZ_DNS_ZONE:-not set}"
echo "Record:   ${AZ_RECORD_NAME:-not set}.${AZ_DNS_ZONE:-}"
echo "Resource Group: ${AZ_RESOURCE_GROUP:-not set}"
echo "=========================="

if [[ -z "${AZ_RESOURCE_GROUP:-}" ]]; then
    echo "FATAL: AZ_RESOURCE_GROUP environment variable is required" >&2
    exit 1
fi

# Copy Azure credentials to a writable location if mounted read-only
AZURE_HOST_CONFIG="${AZURE_HOST_CONFIG:-/host-azure}"
AZURE_CONFIG_DIR="${AZURE_CONFIG_DIR:-${HOME}/.azure}"

if [[ -d "$AZURE_HOST_CONFIG" ]] && [[ -f "$AZURE_HOST_CONFIG/config" ]]; then
    echo "Copying Azure config from $AZURE_HOST_CONFIG to $AZURE_CONFIG_DIR..."
    mkdir -p "$AZURE_CONFIG_DIR"
    # Copy only essential files (skip commands/ log dir — may have permission issues across UIDs)
    for f in config msal_token_cache.json azureProfile.json; do
        cp "$AZURE_HOST_CONFIG/$f" "$AZURE_CONFIG_DIR/" 2>/dev/null || true
    done
    chmod -R u-w "$AZURE_CONFIG_DIR"
    export AZURE_CONFIG_DIR
fi

# Handle graceful shutdown
shutdown=false
trap 'echo "Shutting down..."; shutdown=true' SIGTERM SIGINT

echo "Starting DDNS loop (every ${INTERVAL}s)..."

while [[ "$shutdown" != "true" ]]; do
    echo ""
    echo "─── DDNS check at $(date -u +'%Y-%m-%dT%H:%M:%SZ') ───"
    
    if "$SCRIPT"; then
        echo "Check completed successfully."
    else
        echo "Check failed (exit code $?). Will retry next cycle." >&2
    fi

    # Sleep in small increments to allow quick shutdown
    remaining="$INTERVAL"
    while [[ "$remaining" -gt 0 && "$shutdown" != "true" ]]; do
        sleep 5
        remaining=$((remaining - 5))
    done
done

echo "Azure DDNS container stopped."

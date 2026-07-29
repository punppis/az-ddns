#!/bin/bash
# Default IP resolver for az-ddns
# Queries IP_SERVICE_URL and writes the public IPv4 address to stdout.
#
# Override by setting IP_SERVICE_COMMAND to a custom script or command.
# This script uses IP_SERVICE_URL (default: https://api.ipify.org).

set -euo pipefail

IP_SERVICE_URL="${IP_SERVICE_URL:-https://api.ipify.org}"
TIMEOUT="${IP_TIMEOUT:-15}"

ip=$(curl -sS --connect-timeout 10 --max-time "$TIMEOUT" --tlsv1.2 --proto =https "$IP_SERVICE_URL" 2>/dev/null)

if [[ -z "$ip" ]]; then
    echo "ERROR: Failed to fetch public IP from $IP_SERVICE_URL" >&2
    exit 1
fi

echo "$ip"

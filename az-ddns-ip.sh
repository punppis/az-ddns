#!/bin/bash
# Default IP service for az-ddns
# Returns the public IPv4 address by querying a web service.
#
# Override by setting IP_SERVICE_BINARY to a custom script that
# writes a plain IPv4 address to stdout.

set -euo pipefail

IP_SERVICE_URL="${IP_SERVICE_URL:-https://api.ipify.org}"
TIMEOUT="${IP_TIMEOUT:-15}"

ip=$(curl -sS --connect-timeout 10 --max-time "$TIMEOUT" --tlsv1.2 --proto =https "$IP_SERVICE_URL" 2>/dev/null)

if [[ -z "$ip" ]]; then
    echo "ERROR: Failed to fetch public IP from $IP_SERVICE_URL" >&2
    exit 1
fi

echo "$ip"

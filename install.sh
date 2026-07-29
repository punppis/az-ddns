#!/bin/bash
# Install script for Azure DDNS
set -euo pipefail

echo "=== Installing Azure DDNS ==="

# Create system user if it doesn't exist
if ! id -u az-ddns &>/dev/null; then
    echo "Creating az-ddns system user..."
    useradd --system --no-create-home --shell /usr/sbin/nologin az-ddns
fi

# Install the script
echo "Installing script to /usr/local/bin/az-ddns.sh..."
cp "$(dirname "$0")/az-ddns.sh" /usr/local/bin/az-ddns.sh
chmod 755 /usr/local/bin/az-ddns.sh

echo "Installing IP helper to /usr/local/bin/az-ddns-ip.sh..."
cp "$(dirname "$0")/az-ddns-ip.sh" /usr/local/bin/az-ddns-ip.sh
chmod 755 /usr/local/bin/az-ddns-ip.sh

# Install config if not already present
if [[ ! -f /etc/az-ddns/az-ddns.conf ]]; then
    echo "Installing config to /etc/az-ddns/az-ddns.conf..."
    mkdir -p /etc/az-ddns
    cp "$(dirname "$0")/az-ddns.conf" /etc/az-ddns/az-ddns.conf
    chown root:az-ddns /etc/az-ddns/az-ddns.conf
    chmod 640 /etc/az-ddns/az-ddns.conf
    echo ">>> EDIT /etc/az-ddns/az-ddns.conf and set AZ_RESOURCE_GROUP <<<"
else
    echo "Config already exists at /etc/az-ddns/az-ddns.conf — not overwriting"
fi

# Create state directory with proper ownership
mkdir -p /var/lib/az-ddns
chown az-ddns:az-ddns /var/lib/az-ddns
chmod 750 /var/lib/az-ddns

# Install systemd units
echo "Installing systemd service and timer..."
cp "$(dirname "$0")/az-ddns.service" /etc/systemd/system/
cp "$(dirname "$0")/az-ddns.timer" /etc/systemd/system/
systemctl daemon-reload

# Enable and start the timer
systemctl enable az-ddns.timer
systemctl start az-ddns.timer

echo ""
echo "=== Installation complete ==="
echo "Timer status:"
systemctl status az-ddns.timer --no-pager
echo ""
echo "Next: Edit /etc/az-ddns/az-ddns.conf with your Azure resource group"
echo "      Then run: sudo -u az-ddns az login"
echo "      Test with: sudo systemctl start az-ddns.service && journalctl -u az-ddns.service -f"

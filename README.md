# Azure DDNS

Dynamic DNS updater for Azure DNS zones. Periodically checks your public IP and updates an Azure DNS A record when it changes. Runs in Docker or as a systemd timer on bare metal.

## Quick Start (Docker)

```bash
# 1. Create your config
cp az-ddns.conf.example az-ddns.conf
# Edit az-ddns.conf — set AZ_RESOURCE_GROUP, AZ_DNS_ZONE, and credentials

# 2a. Option A: Mount host Azure CLI credentials
docker compose up -d

# 2b. Option B: Use a Service Principal (no Azure CLI needed)
# Uncomment and fill AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID in az-ddns.conf
# Then comment out the ~/.azure volume mount in docker-compose.yml
docker compose up -d
```

## Quick Start (systemd — Linux)

```bash
sudo ./install.sh
sudo nano /etc/az-ddns/az-ddns.conf   # set AZ_RESOURCE_GROUP and AZ_DNS_ZONE
sudo -u az-ddns az login               # authenticate as the az-ddns user
sudo systemctl start az-ddns.service   # test run
journalctl -u az-ddns.service -f       # watch logs
```

The timer runs every 5 minutes by default. Adjust `OnUnitActiveSec` in `az-ddns.timer` to change the interval.

## Configuration

All settings go in `az-ddns.conf` (copy from `az-ddns.conf.example`):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AZ_RESOURCE_GROUP` | **yes** | — | Azure resource group with your DNS zone |
| `AZ_DNS_ZONE` | **yes** | `changeme.example.com` | DNS zone name |
| `AZ_RECORD_NAME` | no | `@` | A record name (use `@` for zone apex) |
| `AZ_RECORD_TTL` | no | `300` | DNS TTL in seconds |
| `IP_SERVICE_COMMAND` | no | `/usr/local/bin/az-ddns-ip.sh` | Shell command that writes an IPv4 to stdout and exits 0 |
| `IP_SERVICE_URL` | no | `https://api.ipify.org` | Web service used by the default `az-ddns-ip.sh` |
| `STATE_FILE` | no | `/var/lib/az-ddns/last_ip.txt` | Persists last IP across restarts |
| `LOG_FILE` | no | (stdout) | Optional log file path |
| `AZURE_CLIENT_ID` | no | — | Service principal client ID (Docker without Azure CLI) |
| `AZURE_CLIENT_SECRET` | no | — | Service principal secret |
| `AZURE_TENANT_ID` | no | — | Service principal tenant ID |
| `INTERVAL` | no | `300` | Polling interval in seconds (Docker only) |

## Authentication

The updater needs permission to read and update DNS A records in your zone.

### Option A: Azure CLI (host or VM)

```bash
az login
# The az-ddns user or container gets credentials via ~/.azure mount
```

Required RBAC role: **DNS Zone Contributor** on the target DNS zone.

### Option B: Service Principal (containers, CI)

```bash
az ad sp create-for-rbac \
  --name az-ddns-sp \
  --role "DNS Zone Contributor" \
  --scopes /subscriptions/YOUR_SUB_ID/resourceGroups/YOUR_RG
```

Then set `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, and `AZURE_TENANT_ID` in `az-ddns.conf`.

## How It Works

1. Fetches current public IP via `IP_SERVICE_BINARY` (or `IP_SERVICE_URL`)
2. Compares against last known IP (stored in `STATE_FILE`)
3. If unchanged → exits (no API call)
4. If changed → queries Azure DNS for current A record value
5. Updates or creates the A record via `az network dns record-set`
6. Stores new IP in `STATE_FILE`

### Custom IP Command

The default `az-ddns-ip.sh` queries `IP_SERVICE_URL`. Override `IP_SERVICE_COMMAND` with any command that writes an IPv4 to stdout and exits 0:

```bash
# curl (must exit 0 on success)
IP_SERVICE_COMMAND="curl -sf ifconfig.me"

# dig
IP_SERVICE_COMMAND="dig +short myip.opendns.com @resolver1.opendns.com"

# Custom script
IP_SERVICE_COMMAND=/usr/local/bin/my-ip-check.sh
```

## Security

- Docker container runs as **non-root** (UID 1001) with **read-only rootfs**
- systemd service runs as dedicated `az-ddns` user
- State directory permissions: `750`
- Config file permissions: `640`
- TLS enforced on IP check (`--tlsv1.2`)
- `az-ddns.conf` is git-ignored by default

Minimum Azure RBAC role: `DNS Zone Contributor` scoped to the target zone only.

## Files

```
az-ddns.sh              Main script
docker-compose.yml      Docker deployment
Dockerfile              Container build
az-ddns.service         systemd oneshot service
az-ddns.timer           systemd timer (5 min interval)
install.sh              Bare-metal installer
az-ddns.conf.example    Config template → copy to az-ddns.conf
```

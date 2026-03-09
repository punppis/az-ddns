# az-ddns

Azure Dynamic DNS updater – keeps Azure DNS A (and optionally CNAME / MX) records in sync with your current public IP.

Written in Python 3, runs on **Windows, Linux, and macOS**.

---

## Quick start

### 1. Create a service principal (one-time)

```powershell
./init.ps1 -SubscriptionId <sub-id>
```

This creates a `DNS Zone Contributor` service principal and prints its credentials.

### 2. Create your config file

```bash
python3 src/az-ddns --init --config dns.json
```

Edit `dns.json` to list the domains you want to manage:

```json
{
    "lastUpdate": "",
    "domains": {
        "office.kitkagames.com": {
            "A":     { "@": "{{IP}}" },
            "CNAME": { "*": "{{DOMAIN}}" }
        },
        "smb.kitkagames.com": {
            "A":     { "@": "{{IP}}" },
            "MX":    { "@": "mail.kitkagames.com" }
        }
    },
    "state": {}
}
```

**Placeholders**

| Placeholder   | Replaced with                                    |
|---------------|--------------------------------------------------|
| `{{IP}}`      | Current public IPv4 address                      |
| `{{DOMAIN}}`  | The domain name currently being processed        |

**Supported record types:** `A`, `CNAME`, `MX`

### 3. Run

```bash
# Run once
python3 src/az-ddns --config dns.json --once

# Loop forever (15-minute interval)
python3 src/az-ddns --config dns.json

# With Docker Compose
docker compose up --build
```

---

## Configuration

### Azure credentials (environment variables)

| Variable                    | Required | Description                                            |
|-----------------------------|----------|--------------------------------------------------------|
| `AZURE_TENANT_ID`           | Yes      | Azure AD tenant ID                                     |
| `AZURE_CLIENT_ID`           | Yes      | Service principal application (client) ID              |
| `AZURE_SUBSCRIPTION_ID`     | Yes      | Azure subscription ID                                  |
| `AZURE_CLIENT_SECRET`       | Yes*     | Service principal secret (plain text)                  |
| `AZURE_CLIENT_SECRET_FILE`  | Yes*     | Path to a file containing the secret (Docker secrets)  |

\* Provide exactly one of `AZURE_CLIENT_SECRET` or `AZURE_CLIENT_SECRET_FILE`.

For local runs, you can place these variables in a `.env` file in the current
working directory or alongside your `dns.json` config; the script will load it
automatically without overriding variables that are already set. Lines may use
`#` for comments; quote values that need literal `#` characters.

### CLI flags

```
python3 az-ddns --help

  --config PATH         Path to dns.json config file (required)
  --once                Run one cycle and exit
  --init                Write a sample config to --config path and exit
  --force               Force-update every record ignoring cached state
  --interval SECONDS    Seconds between update cycles (default: 900)
  --ttl SECONDS         DNS record TTL (default: 3600)
  --mx-preference N     MX preference value (default: 10)
  --log-level LEVEL     DEBUG / INFO / WARNING / ERROR (default: INFO)
```

---

## Config file (`dns.json`)

The config file is both the **input** (domain/record configuration) and the
**state store** (last-known values written back after each successful update).

```json
{
    "lastUpdate": "2024-06-01T12:00:00Z",
    "domains": {
        "office.kitkagames.com": {
            "A":     { "@":  "{{IP}}" },
            "CNAME": { "*":  "{{DOMAIN}}" },
            "MX":    { "@":  "mail.kitkagames.com" }
        }
    },
    "state": {
        "office.kitkagames.com": {
            "A":     { "@":  "12.3.4.5" },
            "CNAME": { "*":  "office.kitkagames.com" },
            "MX":    { "@":  "mail.kitkagames.com" }
        }
    }
}
```

The `state` section is managed automatically – do not edit it manually.

---

## Behaviour

| Condition                               | Action                                              |
|-----------------------------------------|-----------------------------------------------------|
| First run (`state` is empty)            | Login to Azure, update every configured record      |
| IP **changed** since last run           | Login to Azure, update every configured record      |
| ≥ 12 h since last successful update    | Login to Azure, do a full verification + update     |
| IP **unchanged**, < 12 h elapsed       | Resolve A records via system DNS; skip az CLI if OK |
| `--force` flag                          | Login to Azure, overwrite every record unconditionally |

The script also automatically discovers the Azure DNS zone and resource group
for each domain via `az network dns zone list`, so no `AZURE_RESOURCE_GROUP`
variable is needed.

---

## Docker Compose

Create a `.env` file in the repository root and fill in your credentials, then:

```bash
docker compose up --build
```

`dns.json` is mounted into the container at `/config/dns.json` and is written
back with updated state after each cycle.

---

## Azure zone discovery

For a domain entry like `"office.kitkagames.com"` the script lists all Azure
DNS zones visible to the service principal and picks the **longest-suffix
match**.  This means:

* If `office.kitkagames.com` is its own Azure DNS zone, it is used directly.
* If only `kitkagames.com` exists as a zone, that zone is used and the records
  are updated within it.

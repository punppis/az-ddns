# az-ddns

Azure Dynamic DNS updater — keeps Azure DNS **A records** in sync with your current public IP via a self-hosted .NET 10 web app with a management GUI.

```
┌─────────────────────────────────────────────────────┐
│  Browser / curl         Docker container             │
│  ──────────────         ────────────────             │
│  GUI  (RBAC login) ──►  Razor Pages dashboard        │
│  curl (X-DDNS-TOKEN)──► POST /api/update             │
│                         GET  /api/list               │
│                         Background service (30 min)  │
│                              │ Azure DNS ARM SDK      │
│                              ▼                       │
│                         Azure DNS zones              │
└─────────────────────────────────────────────────────┘
```

---

## Quick start

### 1. Bootstrap

**Linux / macOS**
```bash
bash init.sh
```

**Windows** (PowerShell)
```powershell
.\init.ps1
```

The bootstrap scripts install any missing tools (Python 3, Azure CLI, Docker, .NET 10 SDK), then launch `init.py` for interactive Azure provisioning.

#### On an Azure VM with a managed identity (recommended)

```bash
python3 init.py --managed-identity
```

This skips service principal creation and RBAC assignment entirely. The container uses the VM's [managed identity](https://learn.microsoft.com/en-us/azure/active-directory/managed-identities-azure-resources/overview) to authenticate to Azure DNS at runtime. Ensure the VM's identity has **DNS Zone Contributor** assigned on the subscription before running.

#### On a local machine or without a managed identity

`init.py` guides you through:
1. Azure login
2. Subscription selection
3. Resource group (default: `dynamic-dns`)
4. Service principal with `DNS Zone Contributor` role (used for DNS ARM operations)
5. DDNS token generation (for API auth without browser login)
6. App registration (optional; enables Azure AD login for the web GUI)
7. DNS zone selection → writes `dns.json`
8. Writes all credentials to `.env`
9. Starts the container: `docker compose up -d --build`

**Direct run** (all tools already installed):
```bash
python3 init.py
```

### 2. Open the dashboard

```
http://localhost:8080
```

- **With Azure AD configured**: sign in with your Microsoft account.
- **Without Azure AD** (`AZURE_APP_CLIENT_ID` not set): API-only mode — use `X-DDNS-TOKEN` for all requests.

---

## How it works

| Component              | Description |
|------------------------|-------------|
| Background service     | Polls Azure DNS every 30 min (configurable), caches A `@` records in memory |
| In-memory cache        | Entries expire at 50% of the DNS TTL; refreshed by the background service |
| State persistence      | Current IP per domain kept in memory; survives restarts via `dns.json` |
| `POST /api/update`     | Compares requested IP to cache; calls Azure DNS ARM only when IP changed |
| GUI                    | Razor Pages dashboard — view domains, trigger updates, add/remove managed zones |

### PoC scope: A @ records only

The current release manages only the **apex A record** (`@`) of each zone, i.e. the zone root (`example.com` → `1.2.3.4`). CNAME and MX support will be added in a future release.

---

## Authentication

### GUI (web dashboard)
Requires Azure AD OIDC login. Created automatically by `init.py` (app registration `az-ddns-gui`). Redirect URI: `http://localhost:8080/signin-oidc`.

### API
Accepts either:
- `X-DDNS-TOKEN: <token>` header (value from `DDNS_TOKEN` in `.env`)
- Azure AD Bearer token (when `AZURE_APP_CLIENT_ID` is configured)

**Curl example:**
```bash
# Update all managed domains to your current IP
curl -s -X POST http://localhost:8080/api/update \
     -H "X-DDNS-TOKEN: <your-ddns-token>" \
     -H "Content-Type: application/json" \
     -d '{}'

# Update to a specific IP
curl -s -X POST http://localhost:8080/api/update \
     -H "X-DDNS-TOKEN: <your-ddns-token>" \
     -H "Content-Type: application/json" \
     -d '{"ip": "1.2.3.4"}'

# List current cached records
curl -s http://localhost:8080/api/list \
     -H "X-DDNS-TOKEN: <your-ddns-token>"
```

---

## API reference

### `POST /api/update`

Updates all managed domains (from `dns.json`) to the given IP.

**Request body** (all fields optional):
```json
{
  "ip": "1.2.3.4"   // omit to use caller's detected remote IP
}
```

**Response:**
```json
{
  "success": true,
  "detectedIp": "1.2.3.4",
  "results": [
    { "domain": "home.example.com", "action": "updated",   "newIp": "1.2.3.4" },
    { "domain": "vpn.example.com",  "action": "unchanged", "newIp": "1.2.3.4" }
  ]
}
```

`action` values: `"updated"` | `"unchanged"` | `"error"`

---

### `GET /api/list`

Returns the current in-memory cache for all managed domains.

**Response:**
```json
{
  "success": true,
  "domains": [
    {
      "domain": "home.example.com",
      "currentIp": "1.2.3.4",
      "ttl": 3600,
      "lastFetched": "2024-06-01T12:00:00Z",
      "lastUpdated": "2024-06-01T12:00:00Z",
      "error": null
    }
  ]
}
```

---

## Configuration

All configuration is via environment variables (loaded from `.env` by `init.py`, and mounted via `docker-compose.yml`).

### Minimal configuration (managed identity on Azure VM)

On an Azure VM with a system-assigned managed identity that has **DNS Zone Contributor** on the subscription, only two variables are needed:

```
DDNS_TOKEN=<random-secret>
```

`AZURE_SUBSCRIPTION_ID` is auto-discovered from the first subscription visible to the managed identity if not set.

### Full variable reference

| Variable                  | Required | Description |
|---------------------------|----------|-------------|
| `DDNS_TOKEN`              | **Yes**  | Secret for `X-DDNS-TOKEN` API header |
| `AZURE_SUBSCRIPTION_ID`   | No†      | Azure subscription ID (auto-discovered when using managed identity) |
| `AZURE_TENANT_ID`         | No*      | Azure AD tenant ID |
| `AZURE_CLIENT_ID`         | No*      | Service principal client ID |
| `AZURE_CLIENT_SECRET`     | No*      | Service principal client secret |
| `AZURE_APP_CLIENT_ID`     | No       | App registration client ID (enables GUI OIDC login) |
| `AZURE_APP_CLIENT_SECRET` | No       | App registration client secret |
| `DNS_POLL_INTERVAL`       | No       | Background poll interval in seconds (default: `1800`) |
| `DNS_TTL`                 | No       | DNS A record TTL in seconds (default: `3600`) |
| `DATA_PATH`               | No       | Path to `dns.json` (default: `/data` in container, `.` locally) |

\* When `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, and `AZURE_CLIENT_SECRET` are all absent, `DefaultAzureCredential` is used — which picks up the VM's managed identity automatically.  
† `AZURE_SUBSCRIPTION_ID` is strongly recommended when a VM has access to multiple subscriptions.

---

## Repository layout

```
az-ddns/
  init.sh / init.ps1    — dependency bootstrap (per platform)
  init.py               — interactive Azure provisioning + dns.json generation
  docker-compose.yml    — container definition (mounts dns.json)
  dns.json              — domain list (created by init.py; editable via GUI)  [git-ignored]
  .env                  — credentials (written by init.py)                     [git-ignored]
  app/
    AzDdns.csproj       — .NET 10 ASP.NET Core project
    Dockerfile          — multi-stage container build
    Program.cs          — app host setup
    Services/
      AzureDnsService.cs    — Azure DNS ARM read/write
      DnsCache.cs           — in-memory cache
      DnsConfigStore.cs     — dns.json read/write
      DnsBackgroundService.cs — periodic DNS poll
    Controllers/
      ApiController.cs      — POST /api/update, GET /api/list
    Pages/
      Index.cshtml          — dashboard
      Domains/Index.cshtml  — manage zones
```

---

## Local development (without Docker)

```bash
# From the repo root — .env and dns.json are in . (current directory)
dotnet run --project app/
```

The app auto-discovers `.env` in the repo root (one directory up from `app/`). `DATA_PATH` defaults to `.` when not running in a container.

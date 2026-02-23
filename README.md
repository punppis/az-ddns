## Configuration (Environment Variables)

### Required (Azure auth)
- `AZURE_TENANT_ID`
- `AZURE_CLIENT_ID`
- `AZURE_SUBSCRIPTION_ID`
- `AZURE_CLIENT_SECRET` **or** `AZURE_CLIENT_SECRET_FILE` (Docker secrets path)

### Required (DNS behavior)
- `AZURE_RESOURCE_GROUP` — resource group that contains the Azure DNS zones
- `DNS_MX_TARGET` — MX exchange to ensure at `@`
- `DNS_CNAME_TARGET` — CNAME value to ensure at `*`

### Optional
- `DNS_A_TARGET` — static IPv4 for `A(@)`. If set, script does not perform public IP lookup.
- `IP_STATE_FILE` — path to store last desired IP (optional; recommended for efficiency)
- `INTERVAL_SECONDS` (default `300`)
- `TTL` (default `3600`)
- `MX_PREFERENCE` (default `10`)
- `FORCE_MX`, `FORCE_CNAME` (`true/1`)
- `BLACKLIST_ZONES` (comma-separated)
- `IP_SERVICES` (comma-separated URLs)
- `RUN_ONCE` (`true/1`) to run once and exit (otherwise loops forever)

## A record policy
The script enforces `A(@)` to equal the desired IPv4 (static from `DNS_A_TARGET` or dynamic public IP).
If the existing A record differs, is missing, or has multiple values, it is replaced with a single value.
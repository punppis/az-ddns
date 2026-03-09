#!/usr/bin/env python3
"""
az-ddns: Azure Dynamic DNS Updater

Usage:
    python3 az-ddns --config dns.json
    python3 az-ddns --config dns.json --once
    python3 az-ddns --config dns.json --init   # create a sample config

Config file (dns.json) format
------------------------------
{
    "lastUpdate": "",
    "domains": {
        "office.example.com": {
            "A":     { "@": "{{IP}}" },
            "CNAME": { "*": "{{DOMAIN}}" },
            "MX":    { "@": "mail.example.com" }
        }
    },
    "state": {}
}

Placeholders in record values:
    {{IP}}     -> replaced with the current public IPv4 address
    {{DOMAIN}} -> replaced with the domain name currently being processed

Azure credentials (environment variables):
    AZURE_TENANT_ID               (required)
    AZURE_CLIENT_ID               (required)
    AZURE_SUBSCRIPTION_ID         (required)
    AZURE_CLIENT_SECRET           (required, or AZURE_CLIENT_SECRET_FILE)
    AZURE_CLIENT_SECRET_FILE      (alternative: path to file containing the secret)

Init behaviour:
    * If .env or dns.json is missing, initialization runs by default.
    * Use --no-init to skip initialization checks.

Behaviour:
    * First run (no "state" in config): update every configured record via az CLI.
    * IP unchanged, < 12 h since last run: verify A records with system DNS only;
      skip az CLI unless a mismatch is detected.
    * IP changed, first run, or >= 12 h elapsed: use az CLI for a full update.
    * Loop forever with a 15-minute interval unless --once is given.
    * --force skips all caching and updates every record unconditionally.
"""

import argparse
import datetime
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("az-ddns")

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------
DEFAULT_INTERVAL_SECONDS = 15 * 60   # 15 minutes
FORCE_CHECK_AFTER_HOURS  = 12        # full az-CLI check after 12 h of no update
DEFAULT_TTL              = 3600
DEFAULT_MX_PREFERENCE    = 10
DEFAULT_RESOURCE_GROUP   = "dns-zones"
DEFAULT_SP_NAME          = "dns-updater-sp"

IP_LOOKUP_SERVICES: List[str] = [
    "https://api.ipify.org?format=text",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip",
]

SAMPLE_CONFIG: dict = {
    "lastUpdate": "",
    "domains": {
        "office.example.com": {
            "A":     {"@": "{{IP}}"},
            "CNAME": {"*": "{{DOMAIN}}"},
        }
    },
    "state": {},
}

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def is_ipv4(s: str) -> bool:
    """Return True if *s* looks like a dotted-decimal IPv4 address."""
    return bool(re.match(r"^(?:\d{1,3}\.){3}\d{1,3}$", s.strip()))


def get_public_ip(services: Optional[List[str]] = None) -> str:
    """Fetch the current public IPv4 from well-known external services."""
    for url in (services or IP_LOOKUP_SERVICES):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "az-ddns/2.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                ip = resp.read().decode().strip()
                if is_ipv4(ip):
                    log.debug("Got public IP %s from %s", ip, url)
                    return ip
        except Exception as exc:  # noqa: BLE001
            log.debug("IP service %s failed: %s", url, exc)
    raise RuntimeError("All public IP lookup services failed")


def parse_quoted_env_value(raw: str) -> Tuple[str, str, bool]:
    """
    Parse a quoted .env value.

    Args:
        raw: Raw value string starting with a quote character.

    Returns a tuple of (value, remainder, closed) where:
    - value: the parsed value inside the quotes
    - remainder: any trailing text after the closing quote
    - closed: whether a closing quote was found

    Escape handling is minimal: a backslash escapes the next character and the
    escaped character is included literally in the output.
    """
    quote = raw[0]
    escaped = False
    parsed: List[str] = []
    for index, char in enumerate(raw[1:], start=1):
        if escaped:
            parsed.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == quote:
            return "".join(parsed), raw[index + 1:], True
        parsed.append(char)
    return "".join(parsed), "", False


def load_dotenv(path: str) -> None:
    """Load environment variables from a .env file without overwriting."""
    if not os.path.isfile(path):
        return

    loaded = 0
    with open(path, encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                log.warning("Skipping .env entry with empty key (%s)", path)
                continue
            if key in os.environ:
                continue
            value = value.strip()
            if value and value[0] in ("'", '"'):
                parsed_value, remainder, closed = parse_quoted_env_value(value)
                if closed:
                    remainder = remainder.strip()
                    if remainder and not remainder.startswith("#"):
                        log.warning(
                            "Ignoring trailing content after quoted .env value for %s (%s)",
                            key,
                            path,
                        )
                else:
                    log.warning(
                        "Unclosed quote in .env value for %s (%s)", key, path
                    )
                value = parsed_value
            else:
                value = value.split("#", 1)[0].rstrip()
            os.environ[key] = value
            loaded += 1

    if loaded:
        log.info("Loaded %d environment variable(s) from %s", loaded, path)


def get_dotenv_paths(config_path: str) -> List[str]:
    """Return the candidate .env paths (cwd first, then config directory)."""
    paths = [os.path.join(os.getcwd(), ".env")]
    config_dir = os.path.dirname(os.path.abspath(config_path))
    config_dotenv = os.path.join(config_dir, ".env")
    if config_dotenv not in paths:
        paths.append(config_dotenv)
    return paths


def select_dotenv_target(dotenv_paths: List[str]) -> str:
    """Pick where to create a new .env file when initializing.

    The target prefers the config directory when both cwd and config paths
    are available.
    """
    return dotenv_paths[-1]


def prompt_value(prompt: str, default: Optional[str] = None) -> Optional[str]:
    """Prompt for a value, returning the default when provided and empty."""
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value if value else default


def prompt_required_value(
    prompt: str,
    arg_name: str,
    default: Optional[str] = None,
    max_attempts: int = 3,
) -> str:
    """Prompt until a non-empty value is provided."""
    if not sys.stdin.isatty() and not default:
        raise RuntimeError(
            f"Required initialization value missing: {prompt}. "
            f"Pass it using the {arg_name} command-line argument."
        )
    attempts = 0
    while True:
        value = prompt_value(prompt, default=default)
        if value:
            return value
        attempts += 1
        if attempts >= max_attempts:
            raise RuntimeError(f"{prompt} is required for initialization.")
        log.warning("%s is required.", prompt)


def resolve_a_record(fqdn: str) -> Optional[str]:
    """Resolve an A record via the system DNS resolver (no az CLI)."""
    try:
        info = socket.getaddrinfo(fqdn.rstrip("."), None, socket.AF_INET)
        if info:
            return info[0][4][0]
    except Exception as exc:  # noqa: BLE001
        log.debug("DNS resolve failed for %s: %s", fqdn, exc)
    return None


# ---------------------------------------------------------------------------
# Config file I/O
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    """Load config from *path*; raises FileNotFoundError if absent."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Run 'python3 az-ddns --init --config {path}' to create a sample."
        )
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_config(path: str, config: dict) -> None:
    """Persist *config* to *path*, updating the 'lastUpdate' timestamp."""
    config = dict(config)
    config["lastUpdate"] = (
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=4)
    log.debug("Config saved to %s", path)


def create_sample_config(path: str) -> None:
    """Write a sample config to *path* (exits early if the file exists)."""
    if os.path.exists(path):
        log.warning("Config file already exists: %s", path)
        return
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(SAMPLE_CONFIG, fh, indent=4)
    print(f"Sample config written to {path}")
    print("Edit it to configure your domains, then run without --init.")


def run_init_checks(
    config_path: str,
    dotenv_paths: List[str],
    subscription_id: Optional[str],
    resource_group: str,
    sp_name: str,
    dns_mx_target: Optional[str],
    dns_cname_target: Optional[str],
    dns_a_target: Optional[str],
) -> bool:
    """Ensure .env and config exist, creating them if needed."""
    init_performed = False
    if not any(os.path.isfile(path) for path in dotenv_paths):
        target_path = select_dotenv_target(dotenv_paths)
        sub_id = subscription_id
        if sub_id is None:
            sub_id = prompt_required_value(
                "Enter Azure Subscription ID",
                "--subscription-id",
            )
        mx_target = dns_mx_target
        if mx_target is None:
            mx_target = prompt_required_value(
                "Enter DNS_MX_TARGET (MX exchange)",
                "--dns-mx-target",
            )
        cname_target = dns_cname_target
        if cname_target is None:
            cname_target = prompt_required_value(
                "Enter DNS_CNAME_TARGET (CNAME target)",
                "--dns-cname-target",
            )
        sp = create_service_principal(sub_id, resource_group, sp_name)
        write_env_file(
            target_path,
            sp,
            sub_id,
            resource_group,
            mx_target,
            cname_target,
            dns_a_target,
        )
        init_performed = True

    if not os.path.exists(config_path):
        create_sample_config(config_path)
        init_performed = True

    if init_performed:
        log.info("Initialization complete. Review files, then re-run.")
    return init_performed


# ---------------------------------------------------------------------------
# Azure helpers
# ---------------------------------------------------------------------------

def _az(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run an az CLI command and return the CompletedProcess."""
    cmd = ["az"] + list(args)
    log.debug("az %s", " ".join(args))
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def create_service_principal(
    subscription_id: str,
    resource_group: str,
    name: str,
) -> dict:
    """Create a DNS Zone Contributor service principal and return its details."""
    log.info("Setting subscription...")
    _az("account", "set", "--subscription", subscription_id)
    scope = f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    log.info("Creating service principal scoped to %s...", scope)
    result = _az(
        "ad", "sp", "create-for-rbac",
        "--name", name,
        "--role", "DNS Zone Contributor",
        "--scopes", scope,
        "--output", "json",
    )
    try:
        sp = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Failed to parse service principal creation response."
        ) from exc
    required_keys = ("tenant", "appId", "password")
    missing_keys = [key for key in required_keys if not sp.get(key)]
    if missing_keys:
        raise RuntimeError(
            "Failed to create service principal. Missing required fields: "
            f"{', '.join(missing_keys)}"
        )
    return sp


def write_env_file(
    path: str,
    sp: dict,
    subscription_id: str,
    resource_group: str,
    dns_mx_target: str,
    dns_cname_target: str,
    dns_a_target: Optional[str] = None,
) -> None:
    """Write a .env file with service principal credentials."""
    lines = [
        f"AZURE_TENANT_ID={sp.get('tenant', '')}",
        f"AZURE_CLIENT_ID={sp.get('appId', '')}",
        f"AZURE_CLIENT_SECRET={sp.get('password', '')}",
        f"AZURE_SUBSCRIPTION_ID={subscription_id}",
        "",
        f"AZURE_RESOURCE_GROUP={resource_group}",
        f"DNS_MX_TARGET={dns_mx_target}",
        f"DNS_CNAME_TARGET={dns_cname_target}",
    ]
    if dns_a_target:
        lines.append(f"DNS_A_TARGET={dns_a_target}")
    lines.extend(
        [
            "",
            "# Optional",
            "# IP_STATE_FILE=/state/last_ip.txt",
            "# INTERVAL_SECONDS=300",
            "# TTL=3600",
            "# MX_PREFERENCE=10",
            "# FORCE_MX=true",
            "# FORCE_CNAME=true",
        ]
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    log.info("Created .env at %s", path)


def get_azure_secret() -> str:
    """Return the Azure client secret from env var or secret file."""
    secret = os.environ.get("AZURE_CLIENT_SECRET", "").strip()
    if secret:
        return secret
    secret_file = os.environ.get("AZURE_CLIENT_SECRET_FILE", "").strip()
    if secret_file and os.path.isfile(secret_file):
        with open(secret_file, encoding="utf-8") as fh:
            return fh.read().strip()
    return ""


def az_login() -> None:
    """Authenticate to Azure using a service-principal from env vars."""
    tenant = os.environ.get("AZURE_TENANT_ID", "").strip()
    client = os.environ.get("AZURE_CLIENT_ID", "").strip()
    sub    = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    secret = get_azure_secret()

    missing = [
        name
        for name, val in [
            ("AZURE_TENANT_ID",            tenant),
            ("AZURE_CLIENT_ID",            client),
            ("AZURE_SUBSCRIPTION_ID",      sub),
            ("AZURE_CLIENT_SECRET[_FILE]", secret),
        ]
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    log.info("Authenticating to Azure (service principal)...")
    _az(
        "login", "--service-principal",
        "--username", client, "--password", secret, "--tenant", tenant,
    )
    _az("account", "set", "--subscription", sub)
    log.info("Azure authentication OK")


def list_all_zones() -> List[dict]:
    """Return every Azure DNS zone reachable with the current credentials."""
    result = _az("network", "dns", "zone", "list", "--output", "json")
    return json.loads(result.stdout)


def find_zone_for_domain(
    domain: str, all_zones: List[dict]
) -> Tuple[str, str]:
    """
    Return *(zone_name, resource_group)* for the Azure DNS zone that best
    matches *domain* (longest-suffix match).

    Example
    -------
    Domain ``office.kitkagames.com`` matches zone ``office.kitkagames.com``
    in preference to ``kitkagames.com``.
    """
    domain_lower = domain.lower().rstrip(".")
    best_name: Optional[str] = None
    best_rg:   Optional[str] = None
    best_len = 0

    for zone in all_zones:
        zone_name = zone["name"].lower().rstrip(".")
        if domain_lower == zone_name or domain_lower.endswith("." + zone_name):
            if len(zone_name) > best_len:
                best_len  = len(zone_name)
                best_name = zone["name"]
                best_rg   = zone["resourceGroup"]

    if not best_name:
        raise RuntimeError(f"No Azure DNS zone found for domain '{domain}'")
    return best_name, best_rg


# ---------------------------------------------------------------------------
# DNS record get / set (via az CLI)
# ---------------------------------------------------------------------------

def get_record_value(
    zone: str, rg: str, rtype: str, name: str
) -> Optional[str]:
    """
    Return the current record value from Azure DNS, or *None* if absent.

    For A records returns the single IPv4 address (None if zero or >1).
    For CNAME returns the target hostname (trailing dot stripped).
    For MX returns the exchange hostname (trailing dot stripped).
    """
    result = _az(
        "network", "dns", "record-set", rtype.lower(), "show",
        "-g", rg, "-z", zone, "-n", name, "-o", "json",
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        obj = json.loads(result.stdout)
        rt  = rtype.upper()
        if rt == "A":
            recs = obj.get("aRecords") or obj.get("arecords") or []
            if len(recs) == 1:
                addr = recs[0].get("ipv4Address") or recs[0].get("ipv4address", "")
                return addr.strip()
        elif rt == "CNAME":
            cr = obj.get("cnameRecord") or obj.get("cnamerecord") or {}
            return (cr.get("cname") or "").strip().rstrip(".")
        elif rt == "MX":
            recs = obj.get("mxRecords") or obj.get("mxrecords") or []
            if recs:
                return (recs[0].get("exchange") or "").strip().rstrip(".")
    except Exception as exc:  # noqa: BLE001
        log.debug("Parsing %s record failed: %s", rtype, exc)
    return None


def set_record(
    zone: str, rg: str, rtype: str, name: str, value: str,
    ttl: int = DEFAULT_TTL, mx_pref: int = DEFAULT_MX_PREFERENCE,
) -> None:
    """
    Replace a DNS record in Azure with a single new value.

    The existing record-set (if any) is deleted first to guarantee a clean
    single-value state.
    """
    rt = rtype.upper()
    # Delete existing (ignore failure — record may not exist yet)
    _az(
        "network", "dns", "record-set", rt.lower(), "delete",
        "-g", rg, "-z", zone, "-n", name, "--yes",
        check=False,
    )
    # Create the empty record-set
    _az(
        "network", "dns", "record-set", rt.lower(), "create",
        "-g", rg, "-z", zone, "-n", name, "--ttl", str(ttl),
    )
    # Add the single record value
    if rt == "A":
        _az(
            "network", "dns", "record-set", "a", "add-record",
            "-g", rg, "-z", zone, "-n", name, "-a", value,
        )
    elif rt == "CNAME":
        _az(
            "network", "dns", "record-set", "cname", "set-record",
            "-g", rg, "-z", zone, "-n", name, "--cname", value,
        )
    elif rt == "MX":
        _az(
            "network", "dns", "record-set", "mx", "add-record",
            "-g", rg, "-z", zone, "-n", name,
            "--exchange", value, "--preference", str(mx_pref),
        )
    else:
        raise ValueError(f"Unsupported record type: {rtype}")

    log.info("  SET %s %r in %s -> %r", rt, name, zone, value)


# ---------------------------------------------------------------------------
# Placeholder resolution
# ---------------------------------------------------------------------------

def resolve_value(template: str, ip: str, domain: str) -> str:
    """Replace ``{{IP}}`` and ``{{DOMAIN}}`` in a record-value template."""
    return template.replace("{{IP}}", ip).replace("{{DOMAIN}}", domain)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def get_stored_ip(domains: dict, state: dict) -> Optional[str]:
    """
    Return the IP address that was last stored for an A record using the
    ``{{IP}}`` placeholder.  Used to detect whether the public IP has
    changed without invoking az CLI.
    """
    for domain, domain_cfg in domains.items():
        for rtype, records in domain_cfg.items():
            if rtype.upper() == "A":
                for name, tpl in records.items():
                    if "{{IP}}" in tpl:
                        stored = state.get(domain, {}).get("A", {}).get(name)
                        if stored and is_ipv4(stored):
                            return stored
    return None


def hours_since_last_update(config: dict) -> float:
    """Return how many hours have elapsed since the ``lastUpdate`` timestamp."""
    last = config.get("lastUpdate", "")
    if not last:
        return float("inf")
    try:
        ts = datetime.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
        return (
            datetime.datetime.now(datetime.timezone.utc) - ts
        ).total_seconds() / 3600.0
    except Exception:  # noqa: BLE001
        return float("inf")


# ---------------------------------------------------------------------------
# DNS verification (no az CLI)
# ---------------------------------------------------------------------------

def verify_records_via_dns(
    domains: dict, state: dict, current_ip: str
) -> bool:
    """
    Check every A record whose desired value resolves to *current_ip* by
    querying the system DNS resolver (no az CLI required).

    Returns *True* if all such records already point to the right IP.
    Any mismatch or resolution failure returns *False*.
    """
    for domain, domain_cfg in domains.items():
        for rtype, records in domain_cfg.items():
            if rtype.upper() != "A":
                continue
            for name, tpl in records.items():
                expected = resolve_value(tpl, current_ip, domain)
                if not is_ipv4(expected):
                    # Template does not produce an IP; skip DNS check
                    continue
                fqdn = domain if name == "@" else f"{name}.{domain}"
                resolved = resolve_a_record(fqdn)
                if resolved != expected:
                    log.info(
                        "DNS check: %s A -> %r (expected %r); update needed",
                        fqdn, resolved, expected,
                    )
                    return False
                log.debug("DNS check OK: %s A -> %s", fqdn, resolved)
    return True


# ---------------------------------------------------------------------------
# Core update logic (per-domain, via az CLI)
# ---------------------------------------------------------------------------

def update_domain(
    domain: str,
    domain_cfg: dict,
    current_ip: str,
    state: dict,
    force: bool,
    ttl: int,
    mx_pref: int,
    all_zones: List[dict],
) -> Tuple[Dict[str, Dict[str, str]], bool]:
    """
    Bring all configured DNS records for *domain* into the desired state.

    Returns the updated per-domain state dict and a flag indicating errors.

    The error flag is True when any record update failed or the zone was missing.
    """
    try:
        zone, rg = find_zone_for_domain(domain, all_zones)
    except RuntimeError as exc:
        log.error("%s", exc)
        return state.get(domain, {}), True

    log.info("  Zone: %s  RG: %s", zone, rg)

    # Deep-copy existing per-domain state
    domain_state: Dict[str, Dict[str, str]] = {
        k: dict(v) for k, v in state.get(domain, {}).items()
    }
    has_update_errors = False

    for rtype, records in domain_cfg.items():
        rt = rtype.upper()
        if rt not in domain_state:
            domain_state[rt] = {}

        for name, tpl in records.items():
            desired = resolve_value(tpl, current_ip, domain)
            stored  = domain_state.get(rt, {}).get(name)

            if not force and stored == desired:
                log.info(
                    "  %s %r in %s: state matches (%r), skipping",
                    rt, name, domain, desired,
                )
                continue

            # Verify against live Azure value
            current_az = get_record_value(zone, rg, rt, name)
            if current_az == desired and not force:
                log.info(
                    "  %s %r in %s: Azure already %r, skipping",
                    rt, name, domain, desired,
                )
                domain_state[rt][name] = desired
                continue

            log.info(
                "  %s %r in %s: %r -> %r",
                rt, name, domain, current_az, desired,
            )
            try:
                set_record(zone, rg, rt, name, desired, ttl=ttl, mx_pref=mx_pref)
                domain_state[rt][name] = desired
            except subprocess.CalledProcessError as exc:
                log.error(
                    "  Failed to set %s %r in %s: %s",
                    rt, name, domain, exc.stderr,
                )
                has_update_errors = True

    return domain_state, has_update_errors


# ---------------------------------------------------------------------------
# Single update cycle
# ---------------------------------------------------------------------------

def run_once(
    config_path: str,
    ttl: int   = DEFAULT_TTL,
    mx_pref: int = DEFAULT_MX_PREFERENCE,
    force: bool  = False,
) -> None:
    """Execute one full DDNS update cycle."""
    config  = load_config(config_path)
    domains = config.get("domains") or {}

    if not domains:
        log.warning("No domains configured; nothing to do.")
        return

    state = config.get("state") or {}

    # ---- Current public IP -----------------------------------------------
    log.info("Fetching current public IP...")
    current_ip = get_public_ip()
    log.info("Current public IP: %s", current_ip)

    # ---- Decide whether az CLI is needed ---------------------------------
    first_run      = not state
    hours_elapsed  = hours_since_last_update(config)
    force_due_time = hours_elapsed >= FORCE_CHECK_AFTER_HOURS
    stored_ip      = get_stored_ip(domains, state)
    ip_changed     = stored_ip is None or stored_ip != current_ip

    log.info(
        "State: first_run=%s  stored_ip=%r  ip_changed=%s  "
        "hours_since_last=%.1f  force_due_time=%s  force_flag=%s",
        first_run, stored_ip, ip_changed,
        hours_elapsed, force_due_time, force,
    )

    need_az = force or first_run or ip_changed or force_due_time

    if not need_az:
        log.info("IP unchanged; verifying records via DNS resolution...")
        if verify_records_via_dns(domains, state, current_ip):
            log.info("All DNS records verified; nothing to update.")
            return
        log.info("DNS mismatch detected; proceeding with az CLI update.")
        need_az = True

    # ---- Full az CLI update ----------------------------------------------
    az_login()
    all_zones = list_all_zones()
    log.info("Found %d Azure DNS zone(s)", len(all_zones))

    new_state: dict = {}
    has_update_errors = False
    failed_domains: List[str] = []
    for domain, domain_cfg in domains.items():
        log.info("Processing domain: %s", domain)
        domain_state, domain_has_errors = update_domain(
            domain, domain_cfg, current_ip, state,
            force=force or first_run or force_due_time,
            ttl=ttl, mx_pref=mx_pref, all_zones=all_zones,
        )
        new_state[domain] = domain_state
        has_update_errors = has_update_errors or domain_has_errors
        if domain_has_errors:
            failed_domains.append(domain)

    if has_update_errors:
        failed_list = ", ".join(failed_domains) if failed_domains else "unknown"
        log.error("Update failed for domain(s): %s; skipping state save.", failed_list)
        return

    config["state"] = new_state
    save_config(config_path, config)
    log.info("Done.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Azure Dynamic DNS Updater",
        prog="az-ddns",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python3 az-ddns --init  --config dns.json\n"
            "  python3 az-ddns --once  --config dns.json\n"
            "  python3 az-ddns         --config dns.json  # loop forever\n"
        ),
    )
    parser.add_argument(
        "--config", required=True,
        help="Path to DNS config JSON file (e.g. dns.json)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run one update cycle then exit",
    )
    init_group = parser.add_mutually_exclusive_group()
    init_group.add_argument(
        "--init", action="store_true",
        help="Initialize missing .env/dns.json and exit",
    )
    init_group.add_argument(
        "--no-init", action="store_true",
        help="Skip initialization checks for .env/dns.json",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Force-update every record regardless of cached state",
    )
    parser.add_argument(
        "--subscription-id",
        help="Azure subscription ID for service principal creation",
    )
    parser.add_argument(
        "--resource-group",
        default=DEFAULT_RESOURCE_GROUP,
        help=f"Resource group for service principal creation (default: {DEFAULT_RESOURCE_GROUP})",
    )
    parser.add_argument(
        "--sp-name",
        default=DEFAULT_SP_NAME,
        help=f"Service principal name (default: {DEFAULT_SP_NAME})",
    )
    parser.add_argument(
        "--dns-mx-target",
        help="Default DNS_MX_TARGET value for .env initialization",
    )
    parser.add_argument(
        "--dns-cname-target",
        help="Default DNS_CNAME_TARGET value for .env initialization",
    )
    parser.add_argument(
        "--dns-a-target",
        help="Optional DNS_A_TARGET value for .env initialization",
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL_SECONDS,
        metavar="SECONDS",
        help=f"Seconds between update cycles (default: {DEFAULT_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--ttl", type=int, default=DEFAULT_TTL,
        help=f"DNS record TTL in seconds (default: {DEFAULT_TTL})",
    )
    parser.add_argument(
        "--mx-preference", type=int, default=DEFAULT_MX_PREFERENCE,
        help=f"MX record preference value (default: {DEFAULT_MX_PREFERENCE})",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    dotenv_paths = get_dotenv_paths(args.config)

    if args.init:
        run_init_checks(
            args.config,
            dotenv_paths,
            args.subscription_id,
            args.resource_group,
            args.sp_name,
            args.dns_mx_target,
            args.dns_cname_target,
            args.dns_a_target,
        )
        return

    if not args.no_init:
        init_performed = run_init_checks(
            args.config,
            dotenv_paths,
            args.subscription_id,
            args.resource_group,
            args.sp_name,
            args.dns_mx_target,
            args.dns_cname_target,
            args.dns_a_target,
        )
        if init_performed:
            return

    for dotenv_path in dotenv_paths:
        load_dotenv(dotenv_path)

    if args.once:
        run_once(
            args.config,
            ttl=args.ttl,
            mx_pref=args.mx_preference,
            force=args.force,
        )
        return

    log.info(
        "Starting watch loop (interval=%ds, force_after=%dh)",
        args.interval, FORCE_CHECK_AFTER_HOURS,
    )
    while True:
        try:
            run_once(
                args.config,
                ttl=args.ttl,
                mx_pref=args.mx_preference,
                force=args.force,
            )
        except KeyboardInterrupt:
            log.info("Interrupted; exiting.")
            break
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Cycle failed: %s", exc,
                exc_info=(logging.getLogger().level <= logging.DEBUG),
            )

        next_run = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=args.interval
        )
        log.info("Next run at %s UTC", next_run.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("Interrupted; exiting.")
            break


if __name__ == "__main__":
    main()

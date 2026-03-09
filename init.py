#!/usr/bin/env python3
"""
az-ddns init script
====================
Interactive, cross-platform Azure setup using the Azure CLI.

Preferred entry points (install system dependencies first):
    bash init.sh                      # Linux / macOS
    .\\init.ps1                        # Windows

Direct run (all tools must already be installed):
    python3 init.py
    python  init.py                   # if 'python' is Python 3 on your system

What it does (all steps are idempotent — safe to re-run):
  1.  Checks that 'az' CLI is installed; offers to install if missing.
  2.  Verifies you are logged in; offers 'az login' if not.
  3.  Lists your subscriptions and lets you choose one.
  4.  Selects or creates the Azure resource group
      (default: dynamic-dns; created if absent).
  5.  Selects or creates the Azure Function App
      (Consumption plan — cheapest, no idle charges).
  6.  Looks for an existing service principal named 'az-ddns-sp';
      creates one if it is missing.
  7.  Assigns the 'DNS Zone Contributor' role on the subscription
      to the SP if that assignment does not already exist.
  8.  Generates a secure random DDNS token (used by the Azure
      Functions endpoint) if one is not already in .env.
  9.  Selects or creates an Azure Cache for Redis instance (optional;
      used for distributed concurrency locking).
  10. Lists Azure DNS zones and lets you select which domains/hostnames
      to manage; writes dns.json (merges if it already exists).
  11. Writes / merges all values into .env (never overwrites
      values you have already set).
  12. If az-functions/AzDdns/ exists, also writes
      az-functions/AzDdns/local.settings.json for local dev.
  13. Prints a status report.

Usage:
    python3 init.py                   # normal interactive run
    python3 init.py --force-new-sp    # delete & recreate the service principal
    python3 init.py --sp-name mysp    # use a custom SP display name
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.resolve()
ENV_FILE = REPO_ROOT / ".env"
DNS_JSON_FILE = REPO_ROOT / "dns.json"
FUNCTIONS_SETTINGS = REPO_ROOT / "az-functions" / "AzDdns" / "local.settings.json"

DNS_ZONE_CONTRIBUTOR_ROLE = "DNS Zone Contributor"
DEFAULT_SP_NAME           = "az-ddns-sp"
DEFAULT_RESOURCE_GROUP    = "dynamic-dns"
DEFAULT_LOCATION          = "eastus"

# Redis SKU options shown to the user (cheapest first).
# Each entry: (display_label, sku, cache_size, approx_monthly_cost)
REDIS_SKU_OPTIONS: list[tuple[str, str, str, str]] = [
    ("Basic  C0  — 256 MB, single node, no replication", "Basic",    "c0", "~$16/month"),
    ("Basic  C1  — 1 GB,   single node, no replication", "Basic",    "c1", "~$54/month"),
    ("Standard C0 — 256 MB, primary+replica",            "Standard", "c0", "~$32/month"),
]

# ANSI colours (disabled on Windows unless terminal supports them)
_USE_COLOUR = sys.stdout.isatty() and sys.platform != "win32" or (
    sys.platform == "win32" and os.environ.get("TERM_PROGRAM") == "vscode"
)


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text


def ok(msg: str) -> None:
    print(_c("32", f"  ✔  {msg}"))


def info(msg: str) -> None:
    print(_c("36", f"  →  {msg}"))


def warn(msg: str) -> None:
    print(_c("33", f"  ⚠  {msg}"))


def err(msg: str) -> None:
    print(_c("31", f"  ✘  {msg}"), file=sys.stderr)


def step(msg: str) -> None:
    print(_c("1;34", f"\n[{msg}]"))


def prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"      {msg}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val or default


def confirm(msg: str, default: bool = True) -> bool:
    choices = "Y/n" if default else "y/N"
    try:
        val = input(f"      {msg} [{choices}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if not val:
        return default
    return val in ("y", "yes")


# ---------------------------------------------------------------------------
# .env I/O
# ---------------------------------------------------------------------------

def load_env(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE pairs from *path*; ignore blank lines and comments."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            # Strip surrounding quotes
            val = val.strip().strip('"').strip("'")
            env[key.strip()] = val
    return env


def write_env(path: Path, values: dict[str, str], comments: dict[str, str] | None = None) -> None:
    """Write KEY=VALUE pairs to *path*, preserving existing comment lines."""
    lines: list[str] = []
    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()

    # Rebuild file: keep existing structure, update/add keys
    written: set[str] = set()
    for raw in existing_lines:
        stripped = raw.strip()
        if stripped.startswith("#") or not stripped:
            lines.append(raw)
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in values:
                lines.append(f"{key}={values[key]}")
                written.add(key)
            else:
                lines.append(raw)

    # Append any new keys not already present
    for key, val in values.items():
        if key not in written:
            if comments and key in comments:
                lines.append(f"\n# {comments[key]}")
            lines.append(f"{key}={val}")

    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# az CLI wrappers
# ---------------------------------------------------------------------------

def _az(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = ["az", *args]
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=check,
    )


def _az_json(*args: str, check: bool = True) -> object:
    result = _az(*args, "--output", "json", check=check)
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def check_az_installed() -> bool:
    try:
        r = _az("version", check=False)
        return r.returncode == 0
    except FileNotFoundError:
        return False


def _az_install_command() -> list[str] | None:
    """
    Return a shell command (as a list) that will install the Azure CLI on the
    current platform, or None if no automatic install is known.

    Supported platforms:
      • Linux with apt-get  — official Microsoft script for Debian/Ubuntu
      • macOS               — brew install azure-cli
      • Windows             — winget install Microsoft.AzureCLI
    """
    if sys.platform == "darwin":
        return ["brew", "install", "azure-cli"]

    if sys.platform == "win32":
        return ["winget", "install", "--id", "Microsoft.AzureCLI", "-e"]

    if sys.platform.startswith("linux"):
        # Official Microsoft installer script for Debian/Ubuntu (requires apt-get).
        # Source: https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-linux
        try:
            subprocess.run(
                ["apt-get", "--version"],
                capture_output=True, check=True,
            )
            return [
                "bash", "-c",
                "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash",
            ]
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

    return None


def install_az_cli() -> bool:
    """
    Offer to install the Azure CLI.  Returns True if az is available after the
    attempt, False otherwise.
    """
    err("'az' CLI not found.")
    print()

    cmd = _az_install_command()
    if cmd is None:
        # No known automatic installer — just point to the docs
        info("Visit https://aka.ms/installazurecli for install instructions.")
        return False

    # Show the user what will be run so there are no surprises
    cmd_display = " ".join(cmd)
    info(f"Install command: {cmd_display}")
    print()

    if not confirm("Install the Azure CLI now?", default=True):
        info("Skipped. Install manually from https://aka.ms/installazurecli")
        return False

    try:
        subprocess.run(cmd, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        err(f"Installation failed: {exc}")
        info("Please install manually from https://aka.ms/installazurecli")
        return False

    # Re-check
    if check_az_installed():
        ok("Azure CLI installed successfully.")
        return True

    err("Azure CLI still not found after install. You may need to restart your terminal or check your PATH configuration.")
    return False


def check_logged_in() -> bool:
    r = _az("account", "show", check=False)
    return r.returncode == 0


def get_subscriptions() -> list[dict]:
    result = _az_json("account", "list", "--all")
    return result if isinstance(result, list) else []


def set_subscription(sub_id: str) -> None:
    _az("account", "set", "--subscription", sub_id)


def get_current_account() -> dict:
    result = _az_json("account", "show")
    return result if isinstance(result, dict) else {}


def get_sp_by_name(name: str) -> Optional[dict]:
    """Return the first SP with the given display name, or None."""
    result = _az_json(
        "ad", "sp", "list",
        "--display-name", name,
        "--query", "[0]",
        check=False,
    )
    if isinstance(result, dict) and result.get("appId"):
        return result
    return None


def create_sp(name: str, subscription_id: str) -> dict:
    """
    Create a service principal with DNS Zone Contributor on the subscription.
    Returns the SP credential dict (appId, password, tenant).
    """
    scope = f"/subscriptions/{subscription_id}"
    result = _az_json(
        "ad", "sp", "create-for-rbac",
        "--name", name,
        "--role", DNS_ZONE_CONTRIBUTOR_ROLE,
        "--scopes", scope,
    )
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected response from 'az ad sp create-for-rbac'")
    return result


def get_role_assignments(sp_object_id: str, subscription_id: str) -> list[dict]:
    scope = f"/subscriptions/{subscription_id}"
    result = _az_json(
        "role", "assignment", "list",
        "--assignee", sp_object_id,
        "--scope", scope,
        "--role", DNS_ZONE_CONTRIBUTOR_ROLE,
        check=False,
    )
    return result if isinstance(result, list) else []


def assign_role(sp_object_id: str, subscription_id: str) -> None:
    scope = f"/subscriptions/{subscription_id}"
    _az(
        "role", "assignment", "create",
        "--assignee-object-id", sp_object_id,
        "--assignee-principal-type", "ServicePrincipal",
        "--role", DNS_ZONE_CONTRIBUTOR_ROLE,
        "--scope", scope,
    )


def delete_sp(app_id: str) -> None:
    _az("ad", "sp", "delete", "--id", app_id, check=False)


def get_sp_object_id(app_id: str) -> str:
    result = _az_json(
        "ad", "sp", "show",
        "--id", app_id,
        "--query", "id",
        check=False,
    )
    return result if isinstance(result, str) else ""


def list_redis_instances() -> list[dict]:
    """Return all Azure Cache for Redis instances in the current subscription."""
    result = _az_json("redis", "list", check=False)
    return result if isinstance(result, list) else []


def get_redis_keys(name: str, resource_group: str) -> dict:
    """Return the primary/secondary access keys for a Redis instance."""
    result = _az_json(
        "redis", "list-keys",
        "--name", name,
        "--resource-group", resource_group,
        check=False,
    )
    return result if isinstance(result, dict) else {}


def build_redis_connection_string(instance: dict, keys: dict) -> str:
    """
    Build a StackExchange.Redis connection string from an 'az redis list' entry
    and its access keys.

    Format: <hostname>:<sslPort>,password=<primaryKey>,ssl=True,abortConnect=False
    """
    host = instance.get("hostName", "")
    port = instance.get("sslPort", 6380)
    key  = keys.get("primaryKey", "")
    return f"{host}:{port},password={key},ssl=True,abortConnect=False"


# ---------------------------------------------------------------------------
# Resource group helpers
# ---------------------------------------------------------------------------

def list_resource_groups() -> list[dict]:
    """Return all resource groups in the current subscription."""
    result = _az_json("group", "list", check=False)
    return result if isinstance(result, list) else []


def resource_group_exists(name: str) -> bool:
    r = _az("group", "exists", "--name", name, check=False)
    return r.returncode == 0 and r.stdout.strip().lower() == "true"


def get_resource_group(name: str) -> dict:
    result = _az_json("group", "show", "--name", name, check=False)
    return result if isinstance(result, dict) else {}


def create_resource_group(name: str, location: str) -> dict:
    result = _az_json("group", "create", "--name", name, "--location", location)
    return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# Storage account helpers (prerequisite for Function App)
# ---------------------------------------------------------------------------

def _sanitize_storage_name(base: str) -> str:
    """Produce a valid Azure storage account name: 3-24 lowercase alphanumeric."""
    cleaned = re.sub(r"[^a-z0-9]", "", base.lower())
    return (cleaned or "ddns")[:24].ljust(3, "0")


def create_storage_account(name: str, resource_group: str, location: str,
                            sku: str = "Standard_LRS") -> dict:
    """Create a storage account. Standard_LRS is the cheapest redundancy option."""
    result = _az_json(
        "storage", "account", "create",
        "--name", name,
        "--resource-group", resource_group,
        "--location", location,
        "--sku", sku,
        "--kind", "StorageV2",
        "--allow-blob-public-access", "false",
    )
    return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# Function App helpers
# ---------------------------------------------------------------------------

def list_function_apps() -> list[dict]:
    """Return all Function Apps in the current subscription."""
    result = _az_json("functionapp", "list", check=False)
    return result if isinstance(result, list) else []


def create_function_app(name: str, resource_group: str, storage_account: str,
                        location: str) -> dict:
    """
    Create an Azure Function App on a Consumption (Y1) plan — cheapest option,
    no idle costs.  Uses dotnet-isolated runtime v10 with Functions v4.
    """
    result = _az_json(
        "functionapp", "create",
        "--name", name,
        "--resource-group", resource_group,
        "--storage-account", storage_account,
        "--consumption-plan-location", location,
        "--runtime", "dotnet-isolated",
        "--runtime-version", "10",
        "--functions-version", "4",
        "--os-type", "Windows",
    )
    return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# Redis create helper
# ---------------------------------------------------------------------------

def create_redis(name: str, resource_group: str, location: str,
                 sku: str = "Basic", cache_size: str = "c0") -> dict:
    """
    Create an Azure Cache for Redis instance.
    Default: Basic C0 (cheapest — 256 MB, single node, no replication).
    WARNING: creation typically takes 10-20 minutes.
    """
    info(f"Creating Redis cache '{name}' ({sku} {cache_size.upper()}).")
    warn("Redis provisioning can take 10-20 minutes — please be patient...")
    result = _az_json(
        "redis", "create",
        "--name", name,
        "--resource-group", resource_group,
        "--location", location,
        "--sku", sku,
        "--vm-size", cache_size,
    )
    return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# local.settings.json writer (Azure Functions)
# ---------------------------------------------------------------------------

def write_local_settings(path: Path, values: dict[str, str]) -> None:
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}

    existing.setdefault("IsEncrypted", False)
    existing.setdefault("Values", {})
    existing["Values"].setdefault("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    existing["Values"].setdefault("FUNCTIONS_WORKER_RUNTIME", "dotnet-isolated")

    # Merge: only set values that are not already present
    for key, val in values.items():
        if not existing["Values"].get(key):
            existing["Values"][key] = val

    path.write_text(json.dumps(existing, indent=4), encoding="utf-8")


# ---------------------------------------------------------------------------
# DNS zone helpers
# ---------------------------------------------------------------------------

def list_dns_zones() -> list[dict]:
    """Return all Azure DNS zones visible with the current credentials."""
    result = _az_json("network", "dns", "zone", "list", "--output", "json", check=False)
    return result if isinstance(result, list) else []


def write_dns_json(path: Path, domains: list[str]) -> None:
    """
    Write (or merge into) a dns.json file.

    Each entry in *domains* gets a default A record ``@ -> {{IP}}``.
    Existing entries are preserved; only missing domain keys are added.
    """
    config: dict = {"lastUpdate": "", "domains": {}, "state": {}}
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    config.setdefault("domains", {})
    config.setdefault("state", {})

    added = []
    for domain in domains:
        if domain not in config["domains"]:
            config["domains"][domain] = {"A": {"@": "{{IP}}"}}
            added.append(domain)

    path.write_text(json.dumps(config, indent=4), encoding="utf-8")
    return added



def select_subscription(subscriptions: list[dict]) -> dict:
    if not subscriptions:
        raise RuntimeError("No Azure subscriptions found. Check 'az account list'.")

    enabled = [s for s in subscriptions if s.get("state", "").lower() == "enabled"]
    if not enabled:
        enabled = subscriptions

    if len(enabled) == 1:
        return enabled[0]

    print()
    print("      Available subscriptions:")
    for i, sub in enumerate(enabled, 1):
        marker = " (current)" if sub.get("isDefault") else ""
        print(f"        {i:2d}. {sub['name']:<45} {sub['id']}{marker}")
    print()

    while True:
        raw = prompt(f"Select subscription (1-{len(enabled)})")
        if re.match(r"^\d+$", raw):
            idx = int(raw) - 1
            if 0 <= idx < len(enabled):
                return enabled[idx]
        # Allow pasting a full subscription ID or name
        for sub in enabled:
            if raw.lower() in (sub["id"].lower(), sub["name"].lower()):
                return sub
        warn("Invalid selection — try again.")


def run(args: argparse.Namespace) -> None:
    sp_name: str = args.sp_name

    print(_c("1;37", "\n╔══════════════════════════════════════╗"))
    print(_c("1;37",   "║        az-ddns  init script          ║"))
    print(_c("1;37",   "╚══════════════════════════════════════╝"))
    print("  Generates .env (and local.settings.json) with all required")
    print("  Azure credentials and a random DDNS token.")

    # ------------------------------------------------------------------
    # 1. az CLI check
    # ------------------------------------------------------------------
    step("1/11  Check az CLI")
    if not check_az_installed():
        if not install_az_cli():
            sys.exit(1)
    ok("az CLI is installed.")

    # ------------------------------------------------------------------
    # 2. Login check
    # ------------------------------------------------------------------
    step("2/11  Azure login")
    if check_logged_in():
        account = get_current_account()
        ok(f"Already logged in as {account.get('user', {}).get('name', '?')} "
           f"(tenant {account.get('tenantId', '?')}).")
    else:
        warn("Not logged in.")
        if confirm("Run 'az login' now?"):
            subprocess.run(["az", "login"], check=True)
        else:
            err("Login is required. Run 'az login' first.")
            sys.exit(1)

    # ------------------------------------------------------------------
    # 3. Subscription selection
    # ------------------------------------------------------------------
    step("3/11  Select subscription")
    subscriptions = get_subscriptions()
    chosen_sub = select_subscription(subscriptions)
    set_subscription(chosen_sub["id"])

    subscription_id: str = chosen_sub["id"]
    tenant_id: str = chosen_sub.get("tenantId", "")
    ok(f"Using: {chosen_sub['name']} ({subscription_id})")
    ok(f"Tenant: {tenant_id}")

    # Load any existing .env values so we can reuse them across all steps
    existing_env = load_env(ENV_FILE)

    # ------------------------------------------------------------------
    # 4. Resource group
    # ------------------------------------------------------------------
    step("4/11  Resource group")
    resource_group_name: str = existing_env.get("AZURE_RESOURCE_GROUP", "")
    location: str = existing_env.get("AZURE_LOCATION", "")

    if resource_group_name and resource_group_exists(resource_group_name):
        if not location:
            rg_info = get_resource_group(resource_group_name)
            location = rg_info.get("location", DEFAULT_LOCATION)
        ok(f"Using resource group '{resource_group_name}' (location: {location}) from .env.")
    else:
        all_rgs = list_resource_groups()

        # Pre-select dynamic-dns if it already exists, otherwise default to 0 (create new)
        default_idx = next(
            (str(i + 1) for i, rg in enumerate(all_rgs) if rg["name"] == DEFAULT_RESOURCE_GROUP),
            "0",
        )

        print()
        print("      Available resource groups:")
        print(f"        {'0':>3}  create new resource group")
        for i, rg in enumerate(all_rgs, 1):
            marker = "  <- default" if rg["name"] == DEFAULT_RESOURCE_GROUP else ""
            print(f"        {i:>3}. {rg['name']:<40} [{rg.get('location', '?')}]{marker}")
        print()

        while True:
            raw = prompt(
                "Select resource group (0=create new"
                + (f", 1-{len(all_rgs)}" if all_rgs else "")
                + ")",
                default=default_idx,
            )
            if raw == "0":
                rg_name = prompt("Resource group name", default=DEFAULT_RESOURCE_GROUP)
                rg_loc  = prompt(
                    "Location (e.g. eastus, westeurope, northeurope)",
                    default=DEFAULT_LOCATION,
                )
                info(f"Creating resource group '{rg_name}' in '{rg_loc}'...")
                create_resource_group(rg_name, rg_loc)
                ok(f"Resource group '{rg_name}' created.")
                resource_group_name = rg_name
                location = rg_loc
                break
            if re.match(r"^\d+$", raw):
                idx = int(raw) - 1
                if 0 <= idx < len(all_rgs):
                    chosen_rg = all_rgs[idx]
                    resource_group_name = chosen_rg["name"]
                    location = chosen_rg.get("location", DEFAULT_LOCATION)
                    ok(f"Using '{resource_group_name}' (location: {location}).")
                    break
            warn("Invalid selection -- try again.")

    # ------------------------------------------------------------------
    # 5. Function App
    # ------------------------------------------------------------------
    step("5/11  Azure Function App")
    function_app_name: str = existing_env.get("FUNCTION_APP_NAME", "")

    if function_app_name:
        ok(f"Function App '{function_app_name}' already set in .env -- keeping.")
    else:
        info("Searching for existing Azure Function Apps...")
        fn_apps = list_function_apps()

        print()
        print("      Available Function Apps:")
        print(f"        {'0':>3}  create new Function App")
        for i, app in enumerate(fn_apps, 1):
            rg  = app.get("resourceGroup", "?")
            loc = app.get("location", "?")
            print(f"        {i:>3}. {app['name']:<40} [rg={rg}  loc={loc}]")
        if fn_apps:
            print(f"        {len(fn_apps) + 1:>3}  skip -- do not configure a Function App now")
        print()
        info("Consumption plan (Y1) is the cheapest option -- no idle charges.")
        print()

        while True:
            skip_idx = str(len(fn_apps) + 1) if fn_apps else ""
            choices = (
                "0=create new"
                + (f", 1-{len(fn_apps)}" if fn_apps else "")
                + (f", {skip_idx}=skip" if skip_idx else "")
            )
            raw = prompt(f"Select Function App ({choices})", default="0")

            if raw == "0":
                app_name = prompt(
                    "Function App name (must be globally unique)",
                    default="az-ddns-fn",
                )
                app_loc = prompt("Location", default=location or DEFAULT_LOCATION)
                default_storage = _sanitize_storage_name(app_name + "store")
                storage_name = prompt(
                    "Storage account name (3-24 lowercase alphanumeric)",
                    default=default_storage,
                )
                storage_name = _sanitize_storage_name(storage_name)
                info(f"Storage SKU : Standard_LRS (cheapest)")
                info(f"Plan        : Consumption Y1 (cheapest, no idle cost)")
                if not confirm(
                    f"Create storage '{storage_name}' + Function App '{app_name}' "
                    f"in '{resource_group_name}'?",
                    default=True,
                ):
                    info("Skipping Function App creation.")
                    break
                info(f"Creating storage account '{storage_name}'...")
                create_storage_account(storage_name, resource_group_name, app_loc)
                ok(f"Storage account '{storage_name}' created.")
                info(f"Creating Function App '{app_name}'...")
                fn_result = create_function_app(app_name, resource_group_name, storage_name, app_loc)
                if fn_result:
                    ok(f"Function App '{app_name}' created.")
                else:
                    warn("Function App creation returned unexpected output -- verify in Azure portal.")
                function_app_name = app_name
                break

            if fn_apps and raw == skip_idx:
                info("Skipping Function App -- you can add it later by re-running init.py.")
                break

            if re.match(r"^\d+$", raw):
                idx = int(raw) - 1
                if 0 <= idx < len(fn_apps):
                    function_app_name = fn_apps[idx]["name"]
                    ok(f"Using existing Function App '{function_app_name}'.")
                    break
            warn("Invalid selection -- try again.")

    # ------------------------------------------------------------------
    # 6. Service principal
    # ------------------------------------------------------------------
    step("6/11  Service principal")
    sp_client_secret: str = ""
    sp_client_id: str = ""

    if args.force_new_sp:
        existing_sp = get_sp_by_name(sp_name)
        if existing_sp:
            info(f"--force-new-sp: deleting existing SP '{sp_name}'...")
            delete_sp(existing_sp["appId"])
            ok("Existing SP deleted.")
        existing_sp = None
    else:
        existing_sp = get_sp_by_name(sp_name)

    if existing_sp:
        sp_client_id = existing_sp.get("appId", "")
        ok(f"Found existing SP '{sp_name}' (appId={sp_client_id}).")

        sp_client_secret = existing_env.get("AZURE_CLIENT_SECRET", "")
        if sp_client_secret:
            ok("Client secret loaded from existing .env.")
        else:
            warn(
                "Existing SP found but no client secret in .env.\n"
                "         Either set AZURE_CLIENT_SECRET in .env manually,\n"
                "         or re-run with --force-new-sp to create a fresh SP."
            )
            new_secret = prompt("Paste the existing client secret (leave blank to skip)")
            if new_secret:
                sp_client_secret = new_secret
    else:
        info(f"Creating service principal '{sp_name}' with role '{DNS_ZONE_CONTRIBUTOR_ROLE}'...")
        sp_creds = create_sp(sp_name, subscription_id)
        sp_client_id     = sp_creds["appId"]
        sp_client_secret = sp_creds["password"]
        tenant_id        = sp_creds.get("tenant", tenant_id)
        ok(f"SP created (appId={sp_client_id}).")

    # ------------------------------------------------------------------
    # 7. Role assignment check
    # ------------------------------------------------------------------
    step("7/11  RBAC -- DNS Zone Contributor")
    sp_object_id = get_sp_object_id(sp_client_id)
    if sp_object_id:
        assignments = get_role_assignments(sp_object_id, subscription_id)
        if assignments:
            ok(f"'{DNS_ZONE_CONTRIBUTOR_ROLE}' role is already assigned.")
        else:
            info(f"Assigning '{DNS_ZONE_CONTRIBUTOR_ROLE}' to SP on subscription...")
            try:
                assign_role(sp_object_id, subscription_id)
                ok("Role assigned.")
            except subprocess.CalledProcessError as exc:
                warn(f"Role assignment failed (you may need Owner/User Access Admin):\n{exc.stderr}")
    else:
        warn("Could not retrieve SP object ID; skipping role check.")

    # ------------------------------------------------------------------
    # 8. DDNS token
    # ------------------------------------------------------------------
    step("8/11  DDNS token")
    ddns_token = existing_env.get("DDNS_TOKEN", "")
    if ddns_token:
        ok("DDNS_TOKEN already set in .env -- keeping existing value.")
    else:
        ddns_token = secrets.token_hex(32)
        ok(f"Generated new DDNS_TOKEN ({len(ddns_token)} hex chars).")

    # ------------------------------------------------------------------
    # 9. Redis (optional -- for distributed concurrency lock)
    # ------------------------------------------------------------------
    step("9/11  Redis (optional -- for distributed concurrency lock)")
    redis_conn_str = existing_env.get("REDIS_CONNECTION_STRING", "")

    if redis_conn_str:
        ok("REDIS_CONNECTION_STRING already set in .env -- keeping existing value.")
    else:
        info("Searching for Azure Cache for Redis instances...")
        redis_instances = list_redis_instances()

        create_idx = len(redis_instances) + 1

        print()
        print("      Redis options:")
        print(f"        {'0':>3}  skip -- use in-process concurrency lock (no Redis needed)")
        for i, inst in enumerate(redis_instances, 1):
            rg   = inst.get("resourceGroup", "?")
            host = inst.get("hostName", "?")
            sku  = inst.get("sku", {}).get("name", "?")
            print(f"        {i:>3}. {inst['name']:<30} {host}  [{sku}  rg={rg}]")
        print(f"        {create_idx:>3}  create new Azure Cache for Redis")
        print()
        info("Basic C0 (~$16/month) is the smallest available Redis tier.")
        print()

        while True:
            choices = (
                "0=skip"
                + (f", 1-{len(redis_instances)} existing" if redis_instances else "")
                + f", {create_idx}=create new"
            )
            raw = prompt(f"Select option ({choices})", default="0")

            if raw == "0":
                info("Skipping Redis -- in-process semaphore will be used.")
                break

            if raw == str(create_idx):
                redis_name = prompt("Redis cache name", default="az-ddns-redis")
                redis_loc  = prompt("Location", default=location or DEFAULT_LOCATION)
                print()
                print("      Redis SKU options (cheapest first):")
                for i, (label, _sku, _size, cost) in enumerate(REDIS_SKU_OPTIONS, 1):
                    marker = "  <- default" if i == 1 else ""
                    print(f"        {i}. {label}  ({cost}){marker}")
                print()
                sku_raw = prompt(f"Select SKU (1-{len(REDIS_SKU_OPTIONS)})", default="1")
                try:
                    _label, chosen_sku, chosen_size, _ = REDIS_SKU_OPTIONS[int(sku_raw) - 1]
                except (ValueError, IndexError):
                    _label, chosen_sku, chosen_size, _ = REDIS_SKU_OPTIONS[0]
                if not confirm(
                    f"Create Redis '{redis_name}' ({chosen_sku} {chosen_size.upper()}) in '{redis_loc}'?",
                    default=True,
                ):
                    info("Skipping Redis creation -- in-process semaphore will be used.")
                    break
                new_redis = create_redis(redis_name, resource_group_name, redis_loc,
                                         chosen_sku, chosen_size)
                if new_redis:
                    ok(f"Redis cache '{redis_name}' created.")
                    redis_keys = get_redis_keys(redis_name, resource_group_name)
                    if redis_keys.get("primaryKey"):
                        redis_conn_str = build_redis_connection_string(new_redis, redis_keys)
                        ok("Redis connection string configured.")
                    else:
                        warn("Could not retrieve Redis key. Set REDIS_CONNECTION_STRING in .env manually.")
                else:
                    warn("Redis creation returned unexpected output. Set REDIS_CONNECTION_STRING in .env manually.")
                break

            if re.match(r"^\d+$", raw):
                idx = int(raw) - 1
                if 0 <= idx < len(redis_instances):
                    chosen_redis = redis_instances[idx]
                    info(f"Fetching access keys for '{chosen_redis['name']}'...")
                    redis_keys = get_redis_keys(
                        chosen_redis["name"],
                        chosen_redis["resourceGroup"],
                    )
                    if not redis_keys.get("primaryKey"):
                        warn("Could not retrieve Redis access key. Skipping.")
                    else:
                        redis_conn_str = build_redis_connection_string(chosen_redis, redis_keys)
                        ok(f"Redis connection string built for '{chosen_redis['name']}'.")
                    break
            warn("Invalid selection -- enter a number.")

    # ------------------------------------------------------------------
    # 10. DNS zones → dns.json
    # ------------------------------------------------------------------
    step("10/11  DNS zones (dns.json)")

    info("Fetching Azure DNS zones visible to the service principal...")
    all_zones = list_dns_zones()

    selected_domains: list[str] = []

    if DNS_JSON_FILE.exists():
        ok(f"dns.json already exists at {DNS_JSON_FILE} — will merge any new entries.")
        try:
            existing_cfg = json.loads(DNS_JSON_FILE.read_text(encoding="utf-8"))
            for d in existing_cfg.get("domains", {}):
                ok(f"  Already configured: {d}")
        except json.JSONDecodeError:
            pass

    if all_zones:
        print()
        print("      Azure DNS zones found in your subscription:")
        for i, zone in enumerate(all_zones, 1):
            rg = zone.get("resourceGroup", "?")
            print(f"        {i:>3}. {zone['name']:<45} [rg={rg}]")
        print()
        info("Enter zone numbers (comma-separated) to add to dns.json,")
        info("or type a custom hostname (e.g. home.example.com), or 0 to skip.")
        print()
    else:
        warn("No Azure DNS zones found (SP may not have DNS Zone Contributor yet).")
        info("You can still enter domain names manually.")
        print()

    while True:
        raw = prompt(
            "Zone numbers, hostname(s), or 0 to skip",
            default="0",
        ).strip()

        if raw == "0":
            info("Skipping dns.json domain selection — edit dns.json manually later.")
            break

        # Collect entries: numbers → zone names, anything else → literal hostname
        entries: list[str] = []
        for token in re.split(r"[,\s]+", raw):
            token = token.strip().strip(".")
            if not token:
                continue
            if re.match(r"^\d+$", token):
                idx = int(token) - 1
                if all_zones and 0 <= idx < len(all_zones):
                    entries.append(all_zones[idx]["name"])
                else:
                    warn(f"  No zone at index {token} — skipping.")
            else:
                # Treat as a literal hostname
                entries.append(token)

        if not entries:
            warn("No valid entries — try again or enter 0 to skip.")
            continue

        selected_domains = entries
        break

    if selected_domains:
        added = write_dns_json(DNS_JSON_FILE, selected_domains)
        if added:
            ok(f"dns.json written to {DNS_JSON_FILE}")
            for d in added:
                ok(f"  Added: {d}  (A record @ → {{{{IP}}}})")
        else:
            ok("dns.json already contains all selected domains — nothing new added.")

    # ------------------------------------------------------------------
    # 11. Write .env and local.settings.json
    # ------------------------------------------------------------------
    step("11/11  Write configuration files")

    env_values: dict[str, str] = {
        "AZURE_TENANT_ID":       tenant_id,
        "AZURE_CLIENT_ID":       sp_client_id,
        "AZURE_SUBSCRIPTION_ID": subscription_id,
        "DDNS_TOKEN":            ddns_token,
    }
    env_comments: dict[str, str] = {
        "AZURE_TENANT_ID":       "Azure AD tenant ID",
        "AZURE_CLIENT_ID":       "Service principal application (client) ID",
        "AZURE_SUBSCRIPTION_ID": "Azure subscription ID",
        "DDNS_TOKEN":            "Secret token required in X-DDNS-TOKEN request header",
    }

    if resource_group_name:
        env_values["AZURE_RESOURCE_GROUP"] = resource_group_name
        env_comments["AZURE_RESOURCE_GROUP"] = "Default Azure resource group for az-ddns resources"
    if location:
        env_values["AZURE_LOCATION"] = location
        env_comments["AZURE_LOCATION"] = "Azure region used for az-ddns resources"
    if function_app_name:
        env_values["FUNCTION_APP_NAME"] = function_app_name
        env_comments["FUNCTION_APP_NAME"] = (
            "Azure Function App name (deploy with: "
            "func azure functionapp publish <name>)"
        )
    if sp_client_secret:
        env_values["AZURE_CLIENT_SECRET"] = sp_client_secret
        env_comments["AZURE_CLIENT_SECRET"] = "Service principal client secret"
    if redis_conn_str:
        env_values["REDIS_CONNECTION_STRING"] = redis_conn_str
        env_comments["REDIS_CONNECTION_STRING"] = (
            "Azure Cache for Redis -- used for distributed concurrency lock (DNS_ key prefix)"
        )

    write_env(ENV_FILE, env_values, env_comments)
    ok(f".env written to {ENV_FILE}")

    # Azure Functions local.settings.json
    if FUNCTIONS_SETTINGS.parent.exists():
        fn_values: dict[str, str] = {
            "AZURE_TENANT_ID":       tenant_id,
            "AZURE_CLIENT_ID":       sp_client_id,
            "AZURE_SUBSCRIPTION_ID": subscription_id,
            "DDNS_TOKEN":            ddns_token,
            "DDNS_CONCURRENCY_TIMEOUT_SECONDS": "30",
            "DNS_TTL":               "3600",
            "DNS_MX_PREFERENCE":     "10",
            "REDIS_CONNECTION_STRING": redis_conn_str,
        }
        if sp_client_secret:
            fn_values["AZURE_CLIENT_SECRET"] = sp_client_secret
        write_local_settings(FUNCTIONS_SETTINGS, fn_values)
        ok(f"local.settings.json written to {FUNCTIONS_SETTINGS}")
    else:
        info("az-functions/ directory not found; skipping local.settings.json.")

    # ------------------------------------------------------------------
    # Status report
    # ------------------------------------------------------------------
    print()
    print(_c("1;37", "  ======================================"))
    print(_c("1;32", "  OK  Initialisation complete!"))
    print(_c("1;37", "  ======================================"))
    print()
    print(f"  Subscription ID : {subscription_id}")
    print(f"  Tenant ID       : {tenant_id}")
    print(f"  Resource group  : {resource_group_name or '<not set>'}")
    print(f"  Location        : {location or '<not set>'}")
    print(f"  Function App    : {function_app_name or '<not set>'}")
    print(f"  Client ID       : {sp_client_id}")
    print(f"  Client secret   : {'<set>' if sp_client_secret else '<NOT SET -- edit .env>'}")
    print(f"  DDNS token      : {ddns_token[:8]}...  (stored in .env)")
    print(f"  Redis           : {'<configured>' if redis_conn_str else '<not configured -- in-process lock>'}")
    print()
    print("  Next steps:")
    print("    * Python updater  :  python3 src/az-ddns --config dns.json --once")
    print("    * Docker Compose  :  docker compose up --build")
    if function_app_name:
        print(f"    * Deploy to Azure :  cd az-functions && func azure functionapp publish {function_app_name}")
    elif FUNCTIONS_SETTINGS.parent.exists():
        print("    * Azure Functions :  cd az-functions/AzDdns && func start")
    print()
    if not sp_client_secret:
        warn("AZURE_CLIENT_SECRET is not set in .env.")
        warn("If you already have the secret, add it manually:")
        warn(f"  echo 'AZURE_CLIENT_SECRET=<secret>' >> {ENV_FILE}")
        warn("Or re-run with --force-new-sp to generate a new SP and secret.")
    print()



def main() -> None:
    parser = argparse.ArgumentParser(
        description="az-ddns interactive init — generates .env via az CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 init.py\n"
            "  python3 init.py --sp-name my-ddns-sp\n"
            "  python3 init.py --force-new-sp\n"
        ),
    )
    parser.add_argument(
        "--sp-name",
        default=DEFAULT_SP_NAME,
        metavar="NAME",
        help=f"Display name for the service principal (default: {DEFAULT_SP_NAME})",
    )
    parser.add_argument(
        "--force-new-sp",
        action="store_true",
        help="Delete and recreate the service principal (generates a fresh secret)",
    )
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()

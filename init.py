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
  5.  (Skipped with --managed-identity) Looks for an existing service principal
      named 'az-ddns-sp'; creates one if missing (used for DNS ARM operations).
  6.  (Skipped with --managed-identity) Assigns the 'DNS Zone Contributor' role
      on the subscription to the SP if that assignment does not already exist.
  7.  Generates a secure random DDNS token (X-DDNS-TOKEN API secret).
  8.  Creates (or reuses) an Azure AD app registration for the web GUI
      OIDC login — skippable; GUI falls back to API-only mode.
  9.  Lists Azure DNS zones and lets you select which domains/hostnames
      to manage; writes dns.json (merges if it already exists).
  10. Writes / merges all values into .env (never overwrites values you
      have already set).
  11. Starts the container: docker compose up -d --build

Managed-identity mode (recommended for Azure VMs):
    python3 init.py --managed-identity
    No service principal or client secret is created. The container uses the
    VM's managed identity to authenticate to Azure DNS at runtime. Only
    DDNS_TOKEN (and optionally AZURE_SUBSCRIPTION_ID) are written to .env.

Usage:
    python3 init.py                    # normal interactive run (creates SP)
    python3 init.py --managed-identity # VM / managed-identity run (no SP)
    python3 init.py --force-new-sp     # delete & recreate the service principal
    python3 init.py --sp-name mysp     # use a custom SP display name
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

DNS_ZONE_CONTRIBUTOR_ROLE = "DNS Zone Contributor"
DEFAULT_SP_NAME           = "az-ddns-sp"
DEFAULT_APP_REG_NAME      = "az-ddns-gui"
DEFAULT_RESOURCE_GROUP    = "dynamic-dns"
DEFAULT_LOCATION          = "eastus"

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
# DNS zone helpers
# ---------------------------------------------------------------------------

def list_dns_zones() -> list[dict]:
    """Return all Azure DNS zones visible with the current credentials."""
    result = _az_json("network", "dns", "zone", "list", check=False)
    return result if isinstance(result, list) else []


def write_dns_json(path: Path, domains: list[str]) -> list[str]:
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


# ---------------------------------------------------------------------------
# Azure AD app registration helpers (GUI OIDC)
# ---------------------------------------------------------------------------

def get_existing_app_registration(display_name: str) -> Optional[dict]:
    """Return the first app registration matching *display_name*, or None."""
    result = _az_json(
        "ad", "app", "list",
        "--display-name", display_name,
        "--query", "[0]",
        check=False,
    )
    return result if isinstance(result, dict) else None


def create_app_registration(display_name: str, redirect_uri: str) -> tuple[str, str]:
    """
    Create an Azure AD app registration for OIDC GUI login.
    Returns (client_id, client_secret).
    """
    app = _az_json(
        "ad", "app", "create",
        "--display-name", display_name,
        "--sign-in-audience", "AzureADMyOrg",
        "--web-redirect-uris", redirect_uri,
        "--enable-id-token-issuance", "true",
        "--enable-access-token-issuance", "false",
    )
    client_id: str = app["appId"]

    # Create the service principal for the app registration
    _az("ad", "sp", "create", "--id", client_id, check=False)

    # Generate a client secret (valid 2 years)
    secret_result = _az_json(
        "ad", "app", "credential", "reset",
        "--id", client_id,
        "--years", "2",
    )
    client_secret: str = secret_result.get("password", "")
    return client_id, client_secret


def reset_app_registration_secret(client_id: str) -> str:
    """Generate a new secret for an existing app registration."""
    result = _az_json(
        "ad", "app", "credential", "reset",
        "--id", client_id,
        "--years", "2",
    )
    return result.get("password", "")



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
    print("  Generates .env and dns.json with all required Azure credentials.")

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
    # 5. Service principal (skipped in managed-identity mode)
    # ------------------------------------------------------------------
    step("5/11  Service principal")
    sp_client_secret: str = ""
    sp_client_id: str = ""

    if args.managed_identity:
        ok("--managed-identity: skipping service principal — VM managed identity will be used at runtime.")
    elif args.force_new_sp:
        existing_sp = get_sp_by_name(sp_name)
        if existing_sp:
            info(f"--force-new-sp: deleting existing SP '{sp_name}'...")
            delete_sp(existing_sp["appId"])
            ok("Existing SP deleted.")
        existing_sp = None
        info(f"Creating service principal '{sp_name}' with role '{DNS_ZONE_CONTRIBUTOR_ROLE}'...")
        sp_creds = create_sp(sp_name, subscription_id)
        sp_client_id     = sp_creds["appId"]
        sp_client_secret = sp_creds["password"]
        tenant_id        = sp_creds.get("tenant", tenant_id)
        ok(f"SP created (appId={sp_client_id}).")
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
                    "         or re-run with --force-new-sp to create a fresh SP.\n"
                    "         Or re-run with --managed-identity to skip SP entirely."
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
    # 6. Role assignment (skipped in managed-identity mode)
    # ------------------------------------------------------------------
    step("6/11  RBAC -- DNS Zone Contributor")
    if args.managed_identity:
        ok("--managed-identity: skipping RBAC assignment.")
        info("Ensure the VM's managed identity has 'DNS Zone Contributor' on the subscription.")
    elif sp_client_id:
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
    else:
        warn("No service principal configured; skipping RBAC check.")

    # ------------------------------------------------------------------
    # 8. DDNS token
    # ------------------------------------------------------------------
    step("7/11  DDNS token")
    ddns_token = existing_env.get("DDNS_TOKEN", "")
    if ddns_token:
        ok("DDNS_TOKEN already set in .env -- keeping existing value.")
    else:
        ddns_token = secrets.token_hex(32)
        ok(f"Generated new DDNS_TOKEN ({len(ddns_token)} hex chars).")

    # ------------------------------------------------------------------
    # 8. App registration (GUI OIDC login) — optional
    # ------------------------------------------------------------------
    step("8/11  App registration (GUI OIDC)")
    app_client_id:     str = existing_env.get("AZURE_APP_CLIENT_ID", "")
    app_client_secret: str = existing_env.get("AZURE_APP_CLIENT_SECRET", "")
    redirect_uri: str = "http://localhost:8080/signin-oidc"

    if app_client_id:
        ok(f"AZURE_APP_CLIENT_ID already in .env (appId={app_client_id}) — keeping.")
        if not app_client_secret:
            warn("No AZURE_APP_CLIENT_SECRET in .env.")
            if confirm("Generate a new secret for the existing app registration?", default=False):
                app_client_secret = reset_app_registration_secret(app_client_id)
                ok("New app registration secret generated.")
    else:
        info(
            "An Azure AD app registration is needed for the web GUI login.\n"
            "         It lets users sign in with their Microsoft account to access the dashboard.\n"
            f"         Redirect URI: {redirect_uri}"
        )
        if confirm(f"Create app registration '{DEFAULT_APP_REG_NAME}' now?", default=True):
            existing_app = get_existing_app_registration(DEFAULT_APP_REG_NAME)
            if existing_app:
                app_client_id = existing_app["appId"]
                ok(f"Found existing app registration '{DEFAULT_APP_REG_NAME}' (appId={app_client_id}).")
                if confirm("Generate a new client secret for it?", default=True):
                    app_client_secret = reset_app_registration_secret(app_client_id)
                    ok("Client secret generated.")
            else:
                info(f"Creating app registration '{DEFAULT_APP_REG_NAME}'...")
                app_client_id, app_client_secret = create_app_registration(
                    DEFAULT_APP_REG_NAME, redirect_uri
                )
                ok(f"App registration created (appId={app_client_id}).")
        else:
            info("Skipping app registration — GUI will run in API-only mode.")
            info("Re-run init.py to add it later.")

    # ------------------------------------------------------------------
    # 10. DNS zones → dns.json
    # ------------------------------------------------------------------
    step("9/11  DNS zones (dns.json)")

    info("Fetching Azure DNS zones visible to your current Azure CLI login...")
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
    # 10. Write .env
    # ------------------------------------------------------------------
    step("10/11  Write configuration files")

    env_values: dict[str, str] = {
        "DDNS_TOKEN":            ddns_token,
        "AZURE_SUBSCRIPTION_ID": subscription_id,
    }
    env_comments: dict[str, str] = {
        "DDNS_TOKEN":            "Secret token required in X-DDNS-TOKEN request header",
        "AZURE_SUBSCRIPTION_ID": "Azure subscription ID (optional — auto-discovered from managed identity if omitted)",
    }

    if not args.managed_identity:
        # SP-based auth — write credential vars
        if tenant_id:
            env_values["AZURE_TENANT_ID"] = tenant_id
            env_comments["AZURE_TENANT_ID"] = "Azure AD tenant ID"
        if sp_client_id:
            env_values["AZURE_CLIENT_ID"] = sp_client_id
            env_comments["AZURE_CLIENT_ID"] = "Service principal application (client) ID"
        if sp_client_secret:
            env_values["AZURE_CLIENT_SECRET"] = sp_client_secret
            env_comments["AZURE_CLIENT_SECRET"] = "Service principal client secret"

    if resource_group_name:
        env_values["AZURE_RESOURCE_GROUP"] = resource_group_name
        env_comments["AZURE_RESOURCE_GROUP"] = "Default Azure resource group for az-ddns resources"
    if location:
        env_values["AZURE_LOCATION"] = location
        env_comments["AZURE_LOCATION"] = "Azure region used for az-ddns resources"
    if app_client_id:
        env_values["AZURE_APP_CLIENT_ID"] = app_client_id
        env_comments["AZURE_APP_CLIENT_ID"] = "Azure AD app registration client ID (GUI OIDC login)"
    if app_client_secret:
        env_values["AZURE_APP_CLIENT_SECRET"] = app_client_secret
        env_comments["AZURE_APP_CLIENT_SECRET"] = "Azure AD app registration client secret (GUI OIDC login)"

    write_env(ENV_FILE, env_values, env_comments)
    ok(f".env written to {ENV_FILE}")

    # ------------------------------------------------------------------
    # 11. Start the container
    # ------------------------------------------------------------------
    step("11/11  Start container")

    compose_file = REPO_ROOT / "docker-compose.yml"
    if not DNS_JSON_FILE.exists():
        warn("dns.json not found — the container will start with no managed domains.")
        warn(f"  Create it via the GUI at http://localhost:8080, or re-run init.py.")
    if not compose_file.exists():
        warn(f"docker-compose.yml not found at {compose_file}; skipping container start.")
    else:
        try:
            import shutil
            if shutil.which("docker") is None:
                warn("'docker' not found in PATH; skipping container start.")
                info("Run manually:  docker compose up -d --build")
            else:
                info("Starting container: docker compose up -d --build ...")
                subprocess.run(
                    ["docker", "compose", "up", "-d", "--build"],
                    cwd=str(REPO_ROOT),
                    check=True,
                )
                ok("Container started.  Dashboard → http://localhost:8080")
        except subprocess.CalledProcessError as exc:
            warn(f"docker compose failed (exit {exc.returncode}). Run manually:")
            warn("  docker compose up -d --build")

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
    if args.managed_identity:
        print(f"  Auth mode       : Managed Identity (no service principal)")
    else:
        print(f"  Client ID (SP)  : {sp_client_id or '<not set>'}")
        print(f"  Client secret   : {'<set>' if sp_client_secret else '<NOT SET>'}")
    print(f"  DDNS token      : {ddns_token[:8]}...  (stored in .env)")
    print(f"  GUI app reg     : {app_client_id or '<not configured -- API-only mode>'}")
    print(f"  dns.json        : {DNS_JSON_FILE if DNS_JSON_FILE.exists() else '<not created>'}")
    print()
    print("  Dashboard  :  http://localhost:8080")
    print("  API update :  curl -X POST http://localhost:8080/api/update")
    print(f"               -H 'X-DDNS-TOKEN: {ddns_token[:8]}...' -H 'Content-Type: application/json'")
    print("               -d '{}'")
    print()
    if not args.managed_identity and not sp_client_secret:
        warn("AZURE_CLIENT_SECRET is not set in .env.")
        warn("If you already have the secret, add it manually:")
        warn(f"  echo 'AZURE_CLIENT_SECRET=<secret>' >> {ENV_FILE}")
        warn("Or re-run with --force-new-sp to generate a new SP and secret.")
        warn("Or re-run with --managed-identity if running on an Azure VM.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="az-ddns interactive init — generates .env via az CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 init.py\n"
            "  python3 init.py --managed-identity\n"
            "  python3 init.py --sp-name my-ddns-sp\n"
            "  python3 init.py --force-new-sp\n"
        ),
    )
    parser.add_argument(
        "--managed-identity",
        action="store_true",
        help=(
            "Skip service principal and RBAC steps. "
            "Use this when running on an Azure VM with a managed identity "
            "that already has DNS Zone Contributor on the subscription."
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
    # --managed-identity and --force-new-sp are mutually exclusive
    if args.managed_identity and args.force_new_sp:
        parser.error("--managed-identity and --force-new-sp are mutually exclusive.")
    run(args)


if __name__ == "__main__":
    main()

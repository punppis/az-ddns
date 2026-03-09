#!/usr/bin/env python3
"""
az-ddns init script
====================
Interactive, cross-platform setup using the Azure CLI.

Run ONCE before first use:
    python3 init.py

What it does (all steps are idempotent — safe to re-run):
  1.  Checks that 'az' CLI is installed.
  2.  Verifies you are logged in; offers 'az login' if not.
  3.  Lists your subscriptions and lets you choose one.
  4.  Reads the tenant ID from the selected subscription.
  5.  Looks for an existing service principal named 'az-ddns-sp';
      creates one if it is missing.
  6.  Assigns the 'DNS Zone Contributor' role on the subscription
      to the SP if that assignment does not already exist.
  7.  Generates a secure random DDNS token (used by the Azure
      Functions endpoint) if one is not already in .env.
  8.  Writes / merges all values into .env (never overwrites
      values you have already set).
  9.  If az-functions/AzDdns/ exists, also writes
      az-functions/AzDdns/local.settings.json for local dev.
  10. Prints a status report.

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
FUNCTIONS_SETTINGS = REPO_ROOT / "az-functions" / "AzDdns" / "local.settings.json"

DNS_ZONE_CONTRIBUTOR_ROLE = "DNS Zone Contributor"
DEFAULT_SP_NAME = "az-ddns-sp"

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
# Main flow
# ---------------------------------------------------------------------------

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
    step("1/8  Check az CLI")
    if not check_az_installed():
        if not install_az_cli():
            sys.exit(1)
    ok("az CLI is installed.")

    # ------------------------------------------------------------------
    # 2. Login check
    # ------------------------------------------------------------------
    step("2/8  Azure login")
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
    step("3/8  Select subscription")
    subscriptions = get_subscriptions()
    chosen_sub = select_subscription(subscriptions)
    set_subscription(chosen_sub["id"])

    subscription_id: str = chosen_sub["id"]
    tenant_id: str = chosen_sub.get("tenantId", "")
    ok(f"Using: {chosen_sub['name']} ({subscription_id})")
    ok(f"Tenant: {tenant_id}")

    # ------------------------------------------------------------------
    # 4. Service principal
    # ------------------------------------------------------------------
    step("4/8  Service principal")
    sp_client_secret: str = ""
    sp_client_id: str = ""

    # Load any existing .env values so we can reuse them
    existing_env = load_env(ENV_FILE)

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

        # Reuse secret from .env if present (az CLI never re-displays it)
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
    # 5. Role assignment check
    # ------------------------------------------------------------------
    step("5/8  RBAC — DNS Zone Contributor")
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
    # 6. DDNS token
    # ------------------------------------------------------------------
    step("6/8  DDNS token")
    ddns_token = existing_env.get("DDNS_TOKEN", "")
    if ddns_token:
        ok("DDNS_TOKEN already set in .env — keeping existing value.")
    else:
        ddns_token = secrets.token_hex(32)
        ok(f"Generated new DDNS_TOKEN ({len(ddns_token)} hex chars).")

    # ------------------------------------------------------------------
    # 7. Redis discovery
    # ------------------------------------------------------------------
    step("7/8  Redis (optional — for distributed concurrency lock)")
    redis_conn_str = existing_env.get("REDIS_CONNECTION_STRING", "")

    if redis_conn_str:
        ok("REDIS_CONNECTION_STRING already set in .env — keeping existing value.")
    else:
        info("Searching for Azure Cache for Redis instances in your subscription...")
        redis_instances = list_redis_instances()

        if not redis_instances:
            info("No Azure Cache for Redis instances found. Skipping.")
            info("The function will use an in-process concurrency lock instead.")
        else:
            # Show selection list with a "skip" option
            print()
            print("      Available Redis instances:")
            print(f"        {'':>3}  {'skip — no Redis':}")
            for i, inst in enumerate(redis_instances, 1):
                rg   = inst.get("resourceGroup", "?")
                host = inst.get("hostName", "?")
                sku  = inst.get("sku", {}).get("name", "?")
                print(f"        {i:>3}. {inst['name']:<30} {host}  [{sku}  rg={rg}]")
            print()

            while True:
                raw = prompt(
                    f"Select Redis instance (1-{len(redis_instances)}, or Enter to skip)",
                    default="0",
                )
                if raw in ("", "0"):
                    info("Skipping Redis — in-process semaphore will be used.")
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
                warn("Invalid selection — enter a number or press Enter to skip.")

    # ------------------------------------------------------------------
    # 8. Write .env and local.settings.json
    # ------------------------------------------------------------------
    step("8/8  Write configuration files")

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

    # Only write the secret if we actually have it (don't blank it out)
    if sp_client_secret:
        env_values["AZURE_CLIENT_SECRET"] = sp_client_secret
        env_comments["AZURE_CLIENT_SECRET"] = "Service principal client secret"

    if redis_conn_str:
        env_values["REDIS_CONNECTION_STRING"] = redis_conn_str
        env_comments["REDIS_CONNECTION_STRING"] = "Azure Cache for Redis — used for distributed concurrency lock (DNS_ key prefix)"

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
    print(_c("1;37", "  ══════════════════════════════════════"))
    print(_c("1;32", "  ✔  Initialisation complete!"))
    print(_c("1;37", "  ══════════════════════════════════════"))
    print()
    print(f"  Subscription ID : {subscription_id}")
    print(f"  Tenant ID       : {tenant_id}")
    print(f"  Client ID       : {sp_client_id}")
    print(f"  Client secret   : {'<set>' if sp_client_secret else '<NOT SET — edit .env>'}")
    print(f"  DDNS token      : {ddns_token[:8]}…  (stored in .env)")
    print(f"  Redis           : {'<configured>' if redis_conn_str else '<not configured — in-process lock>'}")
    print()
    print("  Next steps:")
    print("    • Python updater  :  python3 src/az-ddns --config dns.json --once")
    print("    • Docker Compose  :  docker compose up --build")
    if FUNCTIONS_SETTINGS.parent.exists():
        print("    • Azure Functions :  cd az-functions/AzDdns && func start")
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

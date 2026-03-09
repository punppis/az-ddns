#!/usr/bin/env python3
"""
run.py — development smoke-test for the az-ddns Azure Functions endpoint
=========================================================================

What it does
------------
1.  Reads DDNS_TOKEN (and optionally AZURE_* creds) from
    az-functions/AzDdns/local.settings.json.
2.  Starts the Azure Functions host in development mode with `func start`
    (skipped when --no-start / --url points at a remote host).
3.  Waits until http://localhost:7071/api/list responds.
4.  Calls GET  /api/list           — prints live DNS zones/records.
5.  Calls POST /api/update         — sends the update payload.
6.  Calls GET  /api/list           — shows the state after the update.
7.  Prints a PASS / FAIL summary and exits with code 0 / 1.

Prerequisites
-------------
  • Azure Functions Core Tools v4   https://aka.ms/azfunc-install
      OR  --no-start --url https://my-func.azurewebsites.net
  • az-functions/AzDdns/local.settings.json
      (copy from local.settings.json.example and fill in your values)

Usage
-----
  python3 run.py
  python3 run.py --domain subdomain.example.com --ip 1.2.3.4
  python3 run.py --no-start --url https://my-func.azurewebsites.net
  python3 run.py --help
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT         = Path(__file__).parent.resolve()
FUNCTIONS_DIR     = REPO_ROOT / "az-functions" / "AzDdns"
LOCAL_SETTINGS    = FUNCTIONS_DIR / "local.settings.json"
LOCAL_SETTINGS_EX = FUNCTIONS_DIR / "local.settings.json.example"

DEFAULT_HOST      = "http://localhost:7071"
READY_TIMEOUT     = 60   # seconds to wait for `func start` to be ready
READY_POLL        = 2    # seconds between readiness polls

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
_USE_COLOUR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text


def ok(msg: str)   -> None: print(_c("32", f"  ✔  {msg}"))
def info(msg: str) -> None: print(_c("36", f"  →  {msg}"))
def warn(msg: str) -> None: print(_c("33", f"  ⚠  {msg}"))
def err(msg: str)  -> None: print(_c("31", f"  ✘  {msg}"), file=sys.stderr)
def step(msg: str) -> None: print(_c("1;34", f"\n[{msg}]"))
def hdr(msg: str)  -> None: print(_c("1;37", msg))


# ---------------------------------------------------------------------------
# local.settings.json helpers
# ---------------------------------------------------------------------------

def load_local_settings() -> dict[str, str]:
    """Return the Values dict from local.settings.json, or {} if missing."""
    if not LOCAL_SETTINGS.exists():
        return {}
    try:
        data = json.loads(LOCAL_SETTINGS.read_text(encoding="utf-8"))
        return {k: str(v) for k, v in data.get("Values", {}).items() if v}
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http(
    method: str,
    url: str,
    token: str,
    body: Optional[dict] = None,
    timeout: int = 30,
) -> tuple[int, Any]:
    """
    Perform an HTTP request and return (status_code, parsed_json_or_text).
    """
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "X-DDNS-TOKEN": token,
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   "az-ddns-run.py/1.0",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode(errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace") if exc.fp else ""
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def wait_for_ready(base_url: str, token: str, timeout: int) -> bool:
    """Poll /api/list until the function host is accepting requests."""
    deadline = time.monotonic() + timeout
    url = f"{base_url}/api/list"
    while time.monotonic() < deadline:
        status, _ = http("GET", url, token, timeout=3)
        if status in (200, 401, 404):   # any HTTP response means host is up
            return True
        time.sleep(READY_POLL)
    return False


# ---------------------------------------------------------------------------
# func start
# ---------------------------------------------------------------------------

def start_func_host() -> subprocess.Popen:
    """
    Start `func start` inside the Azure Functions project directory.
    Returns the Popen object so the caller can terminate it later.
    """
    cmd = ["func", "start", "--port", "7071"]
    info(f"Starting Azure Functions host: {' '.join(cmd)}")
    info(f"  Working directory: {FUNCTIONS_DIR}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(FUNCTIONS_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # New process group so we can kill it cleanly on all platforms
        **( {"start_new_session": True} if sys.platform != "win32"
            else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} ),
    )
    return proc


def stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    info("Stopping Azure Functions host...")
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


# ---------------------------------------------------------------------------
# Print helpers
# ---------------------------------------------------------------------------

def print_response(status: int, body: Any) -> None:
    colour = "32" if status == 200 else "31" if status >= 400 else "33"
    print(_c(colour, f"  HTTP {status}"))
    print(json.dumps(body, indent=2) if isinstance(body, (dict, list)) else str(body))


def build_update_payload(domain: str, ip: Optional[str]) -> dict:
    """Build a minimal update request payload."""
    record_value = ip if ip else "{{IP}}"
    return {
        "ip":      ip,          # None means "use detected client IP"
        "records": {
            domain: {
                "A": {"@": record_value}
            }
        }
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Smoke-test the az-ddns Azure Functions endpoint.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 run.py\n"
            "  python3 run.py --domain subdomain.example.com --ip 1.2.3.4\n"
            "  python3 run.py --no-start --url https://my-func.azurewebsites.net\n"
        ),
    )
    p.add_argument(
        "--url",
        default=DEFAULT_HOST,
        help=f"Base URL of the Azure Functions host (default: {DEFAULT_HOST})",
    )
    p.add_argument(
        "--no-start",
        action="store_true",
        help="Do not start a local func host; use --url to point at a running one",
    )
    p.add_argument(
        "--domain",
        default=None,
        metavar="FQDN",
        help="Domain to update in the POST /api/update call (e.g. subdomain.example.com)",
    )
    p.add_argument(
        "--ip",
        default=None,
        metavar="IP",
        help="Explicit IPv4 to set; omit to let the function detect the client IP",
    )
    p.add_argument(
        "--token",
        default=None,
        metavar="TOKEN",
        help="DDNS token (overrides local.settings.json / DDNS_TOKEN env var)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=READY_TIMEOUT,
        metavar="SEC",
        help=f"Seconds to wait for the function host to start (default: {READY_TIMEOUT})",
    )
    return p.parse_args()


def run() -> int:
    args = parse_args()

    hdr("\n╔══════════════════════════════════════╗")
    hdr(  "║      az-ddns  run / smoke-test       ║")
    hdr(  "╚══════════════════════════════════════╝\n")

    # ------------------------------------------------------------------
    # 1. Load configuration
    # ------------------------------------------------------------------
    step("1/4  Load configuration")

    settings = load_local_settings()

    # Token: CLI arg > env var > local.settings.json
    token = (
        args.token
        or os.environ.get("DDNS_TOKEN", "")
        or settings.get("DDNS_TOKEN", "")
    )

    if not token:
        err("DDNS_TOKEN is not set.")
        if LOCAL_SETTINGS.exists():
            err(f"  Edit {LOCAL_SETTINGS} and set DDNS_TOKEN.")
        elif LOCAL_SETTINGS_EX.exists():
            err(f"  Copy {LOCAL_SETTINGS_EX} to {LOCAL_SETTINGS} and fill in your values.")
        else:
            err("  Run python3 init.py first to generate credentials.")
        return 1

    ok(f"DDNS_TOKEN loaded ({token[:4]}…).")

    # Domain to update
    domain = args.domain or settings.get("DDNS_TEST_DOMAIN", "")
    if not domain:
        warn("No --domain provided. The /api/update call will be skipped.")
        warn("  Pass --domain subdomain.example.com to enable it.")

    base_url = args.url.rstrip("/")
    info(f"Target: {base_url}")

    # ------------------------------------------------------------------
    # 2. Start local func host (unless --no-start)
    # ------------------------------------------------------------------
    proc: Optional[subprocess.Popen] = None

    if not args.no_start and base_url.startswith("http://localhost"):
        step("2/4  Start Azure Functions host")

        if not FUNCTIONS_DIR.exists():
            err(f"Functions directory not found: {FUNCTIONS_DIR}")
            return 1

        if not LOCAL_SETTINGS.exists():
            err(f"local.settings.json not found at {LOCAL_SETTINGS}")
            if LOCAL_SETTINGS_EX.exists():
                err(f"  Copy it from: {LOCAL_SETTINGS_EX}")
                err(f"  cp {LOCAL_SETTINGS_EX} {LOCAL_SETTINGS}")
            return 1

        # Check that func CLI is available
        try:
            subprocess.run(
                ["func", "--version"],
                capture_output=True, check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            err("'func' CLI not found. Install Azure Functions Core Tools:")
            err("  https://aka.ms/azfunc-install")
            err("  Or use --no-start --url <remote-url> to skip local start.")
            return 1

        proc = start_func_host()

        info(f"Waiting up to {args.timeout}s for host to be ready...")
        ready = wait_for_ready(base_url, token, args.timeout)

        if not ready:
            # Dump whatever the process printed
            if proc.stdout:
                output = proc.stdout.read() if proc.poll() is not None else ""
                if output:
                    print(output)
            err("Function host did not become ready in time.")
            stop_proc(proc)
            return 1

        ok("Function host is ready.")
    else:
        step("2/4  Using existing host (--no-start)")
        ok(f"Skipping local start; targeting {base_url}")

    # ------------------------------------------------------------------
    # 3. Run requests
    # ------------------------------------------------------------------
    step("3/4  Run API calls")
    results: dict[str, bool] = {}

    try:
        # --- GET /api/list (before) ---
        list_url = f"{base_url}/api/list"
        info(f"GET  {list_url}")
        status, body = http("GET", list_url, token)
        print_response(status, body)
        results["GET /api/list (before)"] = status == 200

        # --- POST /api/update ---
        if domain:
            update_url = f"{base_url}/api/update"
            payload = build_update_payload(domain, args.ip)
            ip_display = args.ip or "<client IP>"
            info(f"POST {update_url}  domain={domain}  ip={ip_display}")
            print("  Payload:")
            print("  " + json.dumps(payload, indent=2).replace("\n", "\n  "))
            status, body = http("POST", update_url, token, body=payload)
            print_response(status, body)
            results["POST /api/update"] = status == 200

            # --- GET /api/list (after) ---
            info(f"GET  {list_url}  (after update)")
            status, body = http("GET", list_url, token)
            print_response(status, body)
            results["GET /api/list (after)"] = status == 200
        else:
            results["POST /api/update"] = None   # skipped

    finally:
        if proc is not None:
            stop_proc(proc)

    # ------------------------------------------------------------------
    # 4. Summary
    # ------------------------------------------------------------------
    step("4/4  Summary")
    all_passed = True
    for name, passed in results.items():
        if passed is None:
            warn(f"SKIP  {name}")
        elif passed:
            ok(f"PASS  {name}")
        else:
            err(f"FAIL  {name}")
            all_passed = False

    print()
    if all_passed:
        hdr(_c("1;32", "  ✔  All checks passed."))
    else:
        hdr(_c("1;31", "  ✘  Some checks failed."))
    print()

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(run())

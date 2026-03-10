#!/usr/bin/env bash
# =============================================================================
# init.sh — az-ddns dependency bootstrap (Linux + macOS)
# =============================================================================
# Installs all local tools needed to develop and deploy az-ddns, then
# hands off to init.py for interactive Azure resource provisioning.
#
# Tools installed (only if missing):
#   • Python 3 + pip
#   • Azure CLI
#   • Docker
#   • .NET 10 SDK
#
# Usage:
#   bash init.sh                      # normal run
#   bash init.sh --force-new-sp       # passes extra args to init.py
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${TMPDIR:-/tmp}/az-ddns-install.log"
: > "$LOG_FILE"   # truncate / create

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
  _OK="\033[32m  ✔  \033[0m"
  _INFO="\033[36m  →  \033[0m"
  _WARN="\033[33m  ⚠  \033[0m"
  _ERR="\033[31m  ✘  \033[0m"
  _STEP_ON="\033[1;34m"
  _RESET="\033[0m"
else
  _OK="  ✔  "
  _INFO="  →  "
  _WARN="  ⚠  "
  _ERR="  ✘  "
  _STEP_ON=""
  _RESET=""
fi

step()  { echo -e "\n${_STEP_ON}[$*]${_RESET}"; }
ok()    { echo -e "${_OK}$*"; }
info()  { echo -e "${_INFO}$*"; }
warn()  { echo -e "${_WARN}$*"; }
err()   { echo -e "${_ERR}$*" >&2; }
die()   { err "$*"; exit 1; }

confirm() {
  # confirm "message" [default: y|n]  → returns 0 (yes) or 1 (no)
  local msg="$1" default="${2:-y}"
  local choices
  [ "$default" = "y" ] && choices="Y/n" || choices="y/N"
  printf "      %s [%s]: " "$msg" "$choices"
  local ans
  read -r ans || ans=""
  ans="${ans:-$default}"
  [[ "$ans" =~ ^[Yy] ]]
}

# ---------------------------------------------------------------------------
# Quiet installer — runs a command silently, reports only pass/fail
# ---------------------------------------------------------------------------
install_quietly() {
  # Usage: install_quietly "description" cmd [args...]
  # For pipelines / multi-command blocks:
  #   install_quietly "desc" bash -c "cmd1 && cmd2 | cmd3"
  local desc="$1"; shift
  info "Installing $desc..."
  if "$@" >>"$LOG_FILE" 2>&1; then
    ok "$desc installed successfully."
  else
    err "$desc installation failed."
    warn "See $LOG_FILE for details."
    return 1
  fi
}

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
OS=""
PKG_MGR=""

if [[ "$OSTYPE" == "darwin"* ]]; then
  OS="macos"
elif [ -f /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  OS="linux"
  if   command -v apt-get &>/dev/null; then PKG_MGR="apt"
  elif command -v dnf     &>/dev/null; then PKG_MGR="dnf"
  elif command -v yum     &>/dev/null; then PKG_MGR="yum"
  fi
else
  die "Unsupported platform. Install dependencies manually, then run: python3 init.py"
fi

# ---------------------------------------------------------------------------
# Homebrew (macOS prerequisite for most installs)
# ---------------------------------------------------------------------------
ensure_brew() {
  if ! command -v brew &>/dev/null; then
    install_quietly "Homebrew" bash -c \
      '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    # Add brew to PATH for Apple Silicon
    if [ -f /opt/homebrew/bin/brew ]; then
      eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
  fi
}

# ---------------------------------------------------------------------------
# Print banner
# ---------------------------------------------------------------------------
echo -e "\n${_STEP_ON}╔══════════════════════════════════════╗${_RESET}"
echo -e "${_STEP_ON}║    az-ddns  dependency bootstrap     ║${_RESET}"
echo -e "${_STEP_ON}╚══════════════════════════════════════╝${_RESET}"
echo "  Platform: $OS${PKG_MGR:+  (package manager: $PKG_MGR)}"
echo "  Each tool is checked first; install is skipped if already present."
echo "  Installer output is logged to $LOG_FILE"

# =============================================================================
# 1.  Python 3 + pip
# =============================================================================
step "1/4  Python 3 + pip"

PYTHON=""

# Prefer python3; fall back to python if it is actually Python 3
if   command -v python3 &>/dev/null; then
  PYTHON="python3"
elif command -v python  &>/dev/null \
     && python -c "import sys; sys.exit(0 if sys.version_info.major == 3 else 1)" 2>/dev/null; then
  PYTHON="python"
fi

if [ -n "$PYTHON" ]; then
  PY_VER=$("$PYTHON" -c "import sys; print('.'.join(map(str, sys.version_info[:2])))")
  ok "Python $PY_VER  ($PYTHON)"
else
  warn "Python 3 not found."
  if confirm "Install Python 3 now?"; then
    if [ "$OS" = "macos" ]; then
      ensure_brew
      install_quietly "Python 3" brew install python3
    elif [ "$PKG_MGR" = "apt" ]; then
      install_quietly "Python 3" bash -c "sudo apt-get update && sudo apt-get install -y python3 python3-pip"
    elif [ "$PKG_MGR" = "dnf" ]; then
      install_quietly "Python 3" sudo dnf install -y python3 python3-pip
    elif [ "$PKG_MGR" = "yum" ]; then
      install_quietly "Python 3" sudo yum install -y python3 python3-pip
    else
      die "Please install Python 3 manually: https://www.python.org/downloads/"
    fi
    PYTHON="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
    [ -n "$PYTHON" ] || die "Python 3 still not found after install. Please check your PATH."
  else
    die "Python 3 is required. Install it from https://www.python.org/downloads/"
  fi
fi

# Check pip (may already be present via python3-pip installed above)
if "$PYTHON" -m pip --version &>/dev/null 2>&1; then
  ok "pip available ($PYTHON -m pip)"
else
  warn "pip not found."
  if confirm "Install pip now?"; then
    if [ "$OS" = "macos" ]; then
      install_quietly "pip" bash -c "curl -fsSL https://bootstrap.pypa.io/get-pip.py | $PYTHON"
    elif [ "$PKG_MGR" = "apt" ]; then
      install_quietly "pip" sudo apt-get install -y python3-pip
    elif [ "$PKG_MGR" = "dnf" ]; then
      install_quietly "pip" sudo dnf install -y python3-pip
    elif [ "$PKG_MGR" = "yum" ]; then
      install_quietly "pip" sudo yum install -y python3-pip
    else
      install_quietly "pip" bash -c "curl -fsSL https://bootstrap.pypa.io/get-pip.py | $PYTHON"
    fi
  else
    warn "Skipping pip."
  fi
fi

# =============================================================================
# 2.  Azure CLI
# =============================================================================
step "2/4  Azure CLI"

if command -v az &>/dev/null; then
  ok "Azure CLI already installed."
else
  warn "Azure CLI not found."
  if confirm "Install Azure CLI now?"; then
    if [ "$OS" = "macos" ]; then
      ensure_brew
      install_quietly "Azure CLI" brew install azure-cli
    elif [ "$PKG_MGR" = "apt" ]; then
      install_quietly "Azure CLI" bash -c "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash"
    elif [ "$PKG_MGR" = "dnf" ]; then
      install_quietly "Azure CLI" bash -c "
        sudo rpm --import https://packages.microsoft.com/keys/microsoft.asc
        printf '[azure-cli]\nname=Azure CLI\nbaseurl=https://packages.microsoft.com/yumrepos/azure-cli\nenabled=1\ngpgcheck=1\ngpgkey=https://packages.microsoft.com/keys/microsoft.asc\n' \
          | sudo tee /etc/yum.repos.d/azure-cli.repo > /dev/null
        sudo dnf install -y azure-cli"
    elif [ "$PKG_MGR" = "yum" ]; then
      install_quietly "Azure CLI" bash -c "
        sudo rpm --import https://packages.microsoft.com/keys/microsoft.asc
        printf '[azure-cli]\nname=Azure CLI\nbaseurl=https://packages.microsoft.com/yumrepos/azure-cli\nenabled=1\ngpgcheck=1\ngpgkey=https://packages.microsoft.com/keys/microsoft.asc\n' \
          | sudo tee /etc/yum.repos.d/azure-cli.repo > /dev/null
        sudo yum install -y azure-cli"
    else
      die "Please install Azure CLI manually: https://aka.ms/installazurecli"
    fi
  else
    warn "Skipping Azure CLI — init.py will prompt again if needed."
  fi
fi

# =============================================================================
# 3.  Docker
# =============================================================================
step "3/4  Docker"

if command -v docker &>/dev/null && docker --version &>/dev/null 2>&1; then
  ok "$(docker --version)"
else
  warn "Docker not found."
  if confirm "Install Docker now?"; then
    if [ "$OS" = "macos" ]; then
      ensure_brew
      install_quietly "Docker Desktop" brew install --cask docker
      ok "Start Docker Desktop from the Applications folder before using 'docker compose'."
    elif [ "$PKG_MGR" = "apt" ]; then
      install_quietly "Docker" bash -c "curl -fsSL https://get.docker.com | sudo sh"
      sudo usermod -aG docker "$USER" 2>/dev/null || true
      ok "Log out and back in (or run 'newgrp docker') to use Docker without sudo."
    elif [ "$PKG_MGR" = "dnf" ]; then
      install_quietly "Docker" bash -c "sudo dnf install -y docker && sudo systemctl enable --now docker"
      sudo usermod -aG docker "$USER" 2>/dev/null || true
    elif [ "$PKG_MGR" = "yum" ]; then
      install_quietly "Docker" bash -c "sudo yum install -y docker && sudo systemctl enable --now docker"
      sudo usermod -aG docker "$USER" 2>/dev/null || true
    else
      die "Please install Docker manually: https://docs.docker.com/get-docker/"
    fi
  else
    warn "Skipping Docker — needed for 'docker compose up --build'."
  fi
fi

# =============================================================================
# 4.  .NET 10 SDK
# =============================================================================
step "4/4  .NET 10 SDK"

if command -v dotnet &>/dev/null; then
  ok ".NET $(dotnet --version 2>/dev/null || echo '(version unknown)')"
else
  warn ".NET SDK not found."
  if confirm "Install .NET 10 SDK now?"; then
    if [ "$OS" = "macos" ]; then
      ensure_brew
      install_quietly ".NET 10 SDK" brew install --cask dotnet-sdk
    else
      install_quietly ".NET 10 SDK" bash -c "
        curl -fsSL https://dot.net/v1/dotnet-install.sh \
          | sudo bash -s -- --channel 10.0 --install-dir /usr/local/share/dotnet
        sudo ln -sf /usr/local/share/dotnet/dotnet /usr/local/bin/dotnet 2>/dev/null || true"
    fi
  else
    warn "Skipping .NET SDK — needed to build the az-ddns container image locally."
  fi
fi

# =============================================================================
# Hand off to init.py
# =============================================================================
echo
echo "  ══════════════════════════════════════"
echo "  All system dependencies checked."
echo "  Launching Azure setup (init.py)…"
echo "  ══════════════════════════════════════"
echo

"$PYTHON" "$SCRIPT_DIR/init.py" "$@"
exit $?

#Requires -Version 5.1
# =============================================================================
# init.ps1 — az-ddns dependency bootstrap (Windows)
# =============================================================================
# Installs all local tools needed to develop and deploy az-ddns, then
# hands off to init.py for interactive Azure resource provisioning.
#
# Tools installed (only if missing):
#   • Python 3
#   • Azure CLI
#   • Docker Desktop
#   • Node.js + npm
#   • Azure Functions Core Tools v4  (via npm)
#   • .NET 8 SDK
#
# Requires winget (ships with Windows 10/11 via App Installer).
# Install App Installer from the Microsoft Store if winget is missing:
#   https://aka.ms/getwinget
#
# Usage:
#   .\init.ps1
#   .\init.ps1 --force-new-sp       # extra args are forwarded to init.py
# =============================================================================

param(
    [Parameter(ValueFromRemainingArguments)]
    [string[]]$PyArgs
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
function Step($msg)  { Write-Host "`n[$msg]"       -ForegroundColor Blue }
function Ok($msg)    { Write-Host "  `u{2714}  $msg" -ForegroundColor Green }
function Info($msg)  { Write-Host "  ->  $msg"     -ForegroundColor Cyan }
function Warn($msg)  { Write-Host "  !   $msg"     -ForegroundColor Yellow }
function Die($msg)   { Write-Host "  X   $msg"     -ForegroundColor Red; exit 1 }

function Confirm-Install($what) {
    $ans = Read-Host "      Install $what now? [Y/n]"
    return ($ans -eq '' -or $ans -match '^[Yy]')
}

function Command-Exists($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Refresh-Path {
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') `
              + ';' `
              + [System.Environment]::GetEnvironmentVariable('Path', 'User')
}

function Winget-Install($id) {
    winget install --id $id -e --accept-source-agreements --accept-package-agreements
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Blue
Write-Host "║    az-ddns  dependency bootstrap     ║" -ForegroundColor Blue
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Blue
Write-Host "  Each tool is checked first; install is skipped if already present."

# ---------------------------------------------------------------------------
# Verify winget
# ---------------------------------------------------------------------------
if (-not (Command-Exists 'winget')) {
    Die "'winget' not found. Install App Installer from the Microsoft Store:`n  https://aka.ms/getwinget"
}

# =============================================================================
# 1.  Python 3
# =============================================================================
Step "1/6  Python 3"

$Python = $null

if (Command-Exists 'python3') {
    $Python = 'python3'
} elseif (Command-Exists 'python') {
    $ver = & python -c "import sys; print(sys.version_info.major)" 2>$null
    if ($ver -eq '3') { $Python = 'python' }
}

if ($Python) {
    $pyVer = & $Python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
    Ok "Python $pyVer  ($Python)"
} else {
    Warn "Python 3 not found."
    if (Confirm-Install 'Python 3') {
        Winget-Install 'Python.Python.3.12'
        Refresh-Path
        if (Command-Exists 'python3')   { $Python = 'python3' }
        elseif (Command-Exists 'python') { $Python = 'python'  }
        if (-not $Python) { Die "Python 3 still not found after install. Check your PATH." }
        Ok "Python installed ($Python)."
    } else {
        Die "Python 3 is required. Install from https://www.python.org/downloads/"
    }
}

# =============================================================================
# 2.  Azure CLI
# =============================================================================
Step "2/6  Azure CLI"

if (Command-Exists 'az') {
    Ok "Azure CLI already installed."
} else {
    Warn "Azure CLI not found."
    if (Confirm-Install 'Azure CLI') {
        Winget-Install 'Microsoft.AzureCLI'
        Refresh-Path
        Ok "Azure CLI installed."
    } else {
        Warn "Skipping Azure CLI — init.py will prompt again if needed."
    }
}

# =============================================================================
# 3.  Docker Desktop
# =============================================================================
Step "3/6  Docker"

if (Command-Exists 'docker') {
    Ok "$(docker --version 2>$null)"
} else {
    Warn "Docker not found."
    if (Confirm-Install 'Docker Desktop') {
        Winget-Install 'Docker.DockerDesktop'
        Ok "Docker Desktop installed. Start it from the Start menu before using 'docker compose'."
    } else {
        Warn "Skipping Docker — needed for 'docker compose up --build'."
    }
}

# =============================================================================
# 4.  Node.js + npm
# =============================================================================
Step "4/6  Node.js + npm"

if ((Command-Exists 'node') -and (Command-Exists 'npm')) {
    Ok "Node $(node --version 2>$null) / npm $(npm --version 2>$null)"
} else {
    Warn "Node.js not found."
    if (Confirm-Install 'Node.js LTS') {
        Winget-Install 'OpenJS.NodeJS.LTS'
        Refresh-Path
        Ok "Node.js installed."
    } else {
        Warn "Skipping Node.js — needed for Azure Functions Core Tools."
    }
}

# =============================================================================
# 5.  Azure Functions Core Tools v4
# =============================================================================
Step "5/6  Azure Functions Core Tools v4"

if (Command-Exists 'func') {
    Ok "Azure Functions Core Tools $(func --version 2>$null)"
} else {
    Warn "Azure Functions Core Tools not found."
    if (Command-Exists 'npm') {
        if (Confirm-Install 'Azure Functions Core Tools v4 (via npm)') {
            npm install -g azure-functions-core-tools@4 --unsafe-perm true
            Ok "Azure Functions Core Tools v4 installed."
        } else {
            Warn "Skipping — needed for local testing with run.py."
        }
    } else {
        Warn "npm not found — cannot install Azure Functions Core Tools."
        Info "Install Node.js first, then:  npm install -g azure-functions-core-tools@4"
    }
}

# =============================================================================
# 6.  .NET 8 SDK
# =============================================================================
Step "6/6  .NET 8 SDK"

if (Command-Exists 'dotnet') {
    Ok ".NET $(dotnet --version 2>$null)"
} else {
    Warn ".NET SDK not found."
    if (Confirm-Install '.NET 8 SDK') {
        Winget-Install 'Microsoft.DotNet.SDK.8'
        Refresh-Path
        Ok ".NET 8 SDK installed."
    } else {
        Warn "Skipping .NET SDK — needed to build/run Azure Functions locally."
    }
}

# =============================================================================
# Hand off to init.py
# =============================================================================
Write-Host ""
Write-Host "  ══════════════════════════════════════"
Write-Host "  All system dependencies checked."
Write-Host "  Launching Azure setup (init.py)..."
Write-Host "  ══════════════════════════════════════"
Write-Host ""

& $Python (Join-Path $ScriptDir "init.py") @PyArgs
exit $LASTEXITCODE

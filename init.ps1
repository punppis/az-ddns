#Requires -Version 5.1
# =============================================================================
# init.ps1 — az-ddns dependency bootstrap (Windows)
# =============================================================================
# Installs all local tools needed to develop and deploy az-ddns, then
# hands off to init.py for interactive Azure resource provisioning.
#
# Tools installed (only if missing):
#   • Python 3 + pip
#   • Azure CLI
#   • Docker Desktop
#   • .NET 10 SDK
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
$LogFile   = Join-Path $env:TEMP "az-ddns-install.log"
"" | Set-Content $LogFile   # truncate / create

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
function Step($msg)  { Write-Host "`n[$msg]"         -ForegroundColor Blue }
function Ok($msg)    { Write-Host "  `u{2714}  $msg" -ForegroundColor Green }
function Info($msg)  { Write-Host "  ->  $msg"       -ForegroundColor Cyan }
function Warn($msg)  { Write-Host "  !   $msg"       -ForegroundColor Yellow }
function Die($msg)   { Write-Host "  X   $msg"       -ForegroundColor Red; exit 1 }

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

# Quiet installer — redirects all output to $LogFile, reports only pass/fail
function Install-Quietly {
    param([string]$Desc, [scriptblock]$Cmd)
    Info "Installing $Desc..."
    try {
        $output = & $Cmd 2>&1
        $output | Add-Content $LogFile
        Ok "$Desc installed successfully."
    } catch {
        Add-Content $LogFile "ERROR: $_"
        Write-Host "  X   $Desc installation failed." -ForegroundColor Red
        Warn "See $LogFile for details."
        throw
    }
}

function Winget-Install($id) {
    $output = winget install --id $id -e --accept-source-agreements --accept-package-agreements 2>&1
    $output | Add-Content $LogFile
    if ($LASTEXITCODE -ne 0) { throw "winget install $id failed (exit $LASTEXITCODE)" }
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Blue
Write-Host "║    az-ddns  dependency bootstrap     ║" -ForegroundColor Blue
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Blue
Write-Host "  Each tool is checked first; install is skipped if already present."
Write-Host "  Installer output is logged to $LogFile"

# ---------------------------------------------------------------------------
# Verify winget
# ---------------------------------------------------------------------------
if (-not (Command-Exists 'winget')) {
    Die "'winget' not found. Install App Installer from the Microsoft Store:`n  https://aka.ms/getwinget"
}

# =============================================================================
# 1.  Python 3 + pip
# =============================================================================
Step "1/4  Python 3 + pip"

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
        Install-Quietly "Python 3" { Winget-Install 'Python.Python.3.12' }
        Refresh-Path
        if (Command-Exists 'python3')    { $Python = 'python3' }
        elseif (Command-Exists 'python') { $Python = 'python'  }
        if (-not $Python) { Die "Python 3 still not found after install. Check your PATH." }
    } else {
        Die "Python 3 is required. Install from https://www.python.org/downloads/"
    }
}

# Check pip (bundled with Python on Windows; verify anyway)
$pipOk = & $Python -m pip --version 2>$null
if ($pipOk) {
    Ok "pip available ($Python -m pip)"
} else {
    Warn "pip not found."
    if (Confirm-Install 'pip') {
        Install-Quietly "pip" {
            $pipScript = Join-Path $env:TEMP "get-pip.py"
            Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $pipScript
            & $Python $pipScript
        }
    } else {
        Warn "Skipping pip."
    }
}

# =============================================================================
# 2.  Azure CLI
# =============================================================================
Step "2/4  Azure CLI"

if (Command-Exists 'az') {
    Ok "Azure CLI already installed."
} else {
    Warn "Azure CLI not found."
    if (Confirm-Install 'Azure CLI') {
        Install-Quietly "Azure CLI" { Winget-Install 'Microsoft.AzureCLI' }
        Refresh-Path
    } else {
        Warn "Skipping Azure CLI — init.py will prompt again if needed."
    }
}

# =============================================================================
# 3.  Docker Desktop
# =============================================================================
Step "3/4  Docker"

if (Command-Exists 'docker') {
    Ok "$(docker --version 2>$null)"
} else {
    Warn "Docker not found."
    if (Confirm-Install 'Docker Desktop') {
        Install-Quietly "Docker Desktop" { Winget-Install 'Docker.DockerDesktop' }
        Ok "Start Docker Desktop from the Start menu before using 'docker compose'."
    } else {
        Warn "Skipping Docker — needed for 'docker compose up --build'."
    }
}

# =============================================================================
# 4.  .NET 10 SDK
# =============================================================================
Step "4/4  .NET 10 SDK"

if (Command-Exists 'dotnet') {
    Ok ".NET $(dotnet --version 2>$null)"
} else {
    Warn ".NET SDK not found."
    if (Confirm-Install '.NET 10 SDK') {
        Install-Quietly ".NET 10 SDK" { Winget-Install 'Microsoft.DotNet.SDK.10' }
        Refresh-Path
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

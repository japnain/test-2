@echo off
REM ╔══════════════════════════════════════════════════════════════╗
REM ║  PolyArb — One-Click Launcher (Windows)                     ║
REM ║  Just double-click this file, or run: run.bat                ║
REM ║  Live mode: run.bat --live                                   ║
REM ╚══════════════════════════════════════════════════════════════╝
setlocal enabledelayedexpansion

echo.
echo     ____        __      ___         __
echo    / __ \____  / /_  __/   ^|  _____/ /_
echo   / /_/ / __ \/ / / / / /^| ^| / ___/ __ \
echo  / ____/ /_/ / / /_/ / ___ ^|/ /  / /_/ /
echo /_/    \____/_/\__, /_/  ^|_/_/  /_.___/
echo               /____/
echo.
echo   Polymarket Arbitrage Bot — One-Click Launcher
echo.

REM ── Step 1: Check Python ──────────────────────────────────────
echo   [*] Checking Python...

set "PYTHON="
where py >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=*" %%i in ('py -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2^>nul') do set "PYVER=%%i"
    for /f "tokens=*" %%i in ('py -3 -c "import sys; print(sys.version_info.minor)" 2^>nul') do set "PYMINOR=%%i"
    if !PYMINOR! geq 11 (
        set "PYTHON=py -3"
    )
)

if "!PYTHON!"=="" (
    where python >nul 2>&1
    if %errorlevel%==0 (
        for /f "tokens=*" %%i in ('python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2^>nul') do set "PYVER=%%i"
        for /f "tokens=*" %%i in ('python -c "import sys; print(sys.version_info.minor)" 2^>nul') do set "PYMINOR=%%i"
        if !PYMINOR! geq 11 (
            set "PYTHON=python"
        )
    )
)

if "!PYTHON!"=="" (
    echo   [X] Python 3.11 or higher is required but was not found.
    echo.
    echo   Download Python from: https://www.python.org/downloads/
    echo   IMPORTANT: Check "Add Python to PATH" during installation!
    echo.
    pause
    exit /b 1
)
echo   [OK] Python !PYVER! found

REM ── Step 2: Virtual Environment ───────────────────────────────
if not exist ".venv" (
    echo   [*] Creating virtual environment (first-time setup)...
    !PYTHON! -m venv .venv
    echo   [OK] Virtual environment created
) else (
    echo   [OK] Virtual environment exists
)

REM Activate
call .venv\Scripts\activate.bat

REM ── Step 3: Install Dependencies ──────────────────────────────
where polyarb >nul 2>&1
if %errorlevel% neq 0 (
    echo   [*] Installing dependencies (this takes a minute, only happens once)...
    pip install --quiet --upgrade pip
    pip install --quiet -e "."
    echo   [OK] All dependencies installed
) else (
    echo   [OK] Dependencies already installed
)

REM ── Step 4: Create logs directory ─────────────────────────────
if not exist "logs" mkdir logs

REM ── Step 5: Wallet Setup ──────────────────────────────────────
if not exist ".env" (
    echo.
    echo   === First-Time Wallet Setup ===
    echo.
    echo   To trade on Polymarket, the bot needs your Polygon wallet private key.
    echo   This is stored locally in a .env file and never sent anywhere.
    echo.
    echo   What you need:
    echo     1. A Polygon wallet with USDC.e (for trades)
    echo     2. A small amount of MATIC (for one-time token approval)
    echo     3. Your wallet's private key (starts with 0x)
    echo.

    set /p "PRIV_KEY=  Paste your private key: "

    if "!PRIV_KEY!"=="" (
        echo   [!] No key entered. Running in dry-run mode (no real trades).
        echo # Wallet not configured — bot runs in dry-run mode only> .env
        echo PRIVATE_KEY=>> .env
    ) else (
        echo PRIVATE_KEY=!PRIV_KEY!> .env
        echo   [OK] Private key saved to .env
    )

    echo.
    echo   Do you need a proxy? (for access from restricted regions)
    echo   Format: socks5://user:pass@host:port
    set /p "PROXY=  Proxy URL (press Enter to skip): "

    if not "!PROXY!"=="" (
        echo PROXY_URL=!PROXY!>> .env
        echo   [OK] Proxy configured
    )

    echo.
    echo   === Setup Complete ===
    echo   To change these later, run: setup_wallet.bat
    echo.
)

REM ── Step 6: Launch ────────────────────────────────────────────
echo   === Launching PolyArb ===
echo.

polyarb %*

pause

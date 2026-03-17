#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════╗
# ║  PolyArb — One-Click Launcher                               ║
# ║  Just run: ./run.sh                                          ║
# ║  Live mode: ./run.sh --live                                  ║
# ╚══════════════════════════════════════════════════════════════╝
set -euo pipefail

# ── Colors ──────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }
info() { echo -e "  ${CYAN}→${NC} $1"; }

# ── Resolve script directory (works even if symlinked) ──────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Banner ──────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}"
cat << 'BANNER'
    ____        __      ___         __
   / __ \____  / /_  __/   |  _____/ /_
  / /_/ / __ \/ / / / / /| | / ___/ __ \
 / ____/ /_/ / / /_/ / ___ |/ /  / /_/ /
/_/    \____/_/\__, /_/  |_/_/  /_.___/
              /____/
BANNER
echo -e "${NC}"
echo -e "  ${BOLD}Polymarket Arbitrage Bot${NC} — One-Click Launcher"
echo ""

# ── Step 1: Check Python ───────────────────────────────────────
info "Checking Python..."

PYTHON=""
MIN_MINOR=9  # Minimum Python 3.9

# Check common Python command names (including Homebrew-specific versions)
for cmd in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        major=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo "0")
        minor=$("$cmd" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo "0")
        if [ "$major" -ge 3 ] && [ "$minor" -ge "$MIN_MINOR" ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

# Also check Homebrew paths directly (Mac)
if [ -z "$PYTHON" ]; then
    for brew_py in /opt/homebrew/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 /opt/homebrew/bin/python3.9; do
        if [ -x "$brew_py" ]; then
            ver=$("$brew_py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
            major=$("$brew_py" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo "0")
            minor=$("$brew_py" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo "0")
            if [ "$major" -ge 3 ] && [ "$minor" -ge "$MIN_MINOR" ]; then
                PYTHON="$brew_py"
                break
            fi
        fi
    done
fi

if [ -z "$PYTHON" ]; then
    fail "Python 3.9 or higher is required but was not found."
    echo ""
    echo -e "  ${BOLD}How to install Python:${NC}"
    echo "    Mac (Homebrew): brew install python@3.12"
    echo "    Ubuntu/Debian:  sudo apt install python3"
    echo "    Other:          https://www.python.org/downloads/"
    echo ""
    echo -e "  ${BOLD}Already installed?${NC} Check with: python3 --version"
    echo "  If Homebrew installed it, try: /opt/homebrew/bin/python3 --version"
    echo ""
    exit 1
fi
ok "Python $ver found ($PYTHON)"

# ── Step 2: Virtual Environment ────────────────────────────────
if [ ! -d ".venv" ]; then
    info "Creating virtual environment (first-time setup)..."
    "$PYTHON" -m venv .venv
    ok "Virtual environment created"
else
    ok "Virtual environment exists"
fi

# Activate
source .venv/bin/activate

# ── Step 3: Install Dependencies ───────────────────────────────
if ! command -v polyarb &>/dev/null 2>&1; then
    info "Installing dependencies (this takes a minute, only happens once)..."
    pip install --quiet --upgrade pip
    pip install --quiet -e "." 2>&1 | tail -1
    ok "All dependencies installed"
else
    ok "Dependencies already installed"
fi

# ── Step 4: Create logs directory ──────────────────────────────
mkdir -p logs

# ── Step 5: Wallet Setup ──────────────────────────────────────
if [ ! -f ".env" ]; then
    echo ""
    echo -e "  ${BOLD}${YELLOW}━━━ First-Time Wallet Setup ━━━${NC}"
    echo ""
    echo -e "  To trade on Polymarket, the bot needs your ${BOLD}Polygon wallet private key${NC}."
    echo "  This is stored locally in a .env file and never sent anywhere."
    echo ""
    echo -e "  ${BOLD}What you need:${NC}"
    echo "    1. A Polygon wallet with USDC.e (for trades)"
    echo "    2. A small amount of MATIC (for one-time token approval)"
    echo "    3. Your wallet's private key (starts with 0x)"
    echo ""

    # Read private key (masked input)
    echo -ne "  ${BOLD}Paste your private key:${NC} "
    read -rs PRIV_KEY
    echo ""

    if [ -z "$PRIV_KEY" ]; then
        warn "No key entered. Running in dry-run mode (no real trades)."
        echo "# Wallet not configured — bot runs in dry-run mode only" > .env
        echo "PRIVATE_KEY=" >> .env
    else
        # Validate format
        if [[ ! "$PRIV_KEY" =~ ^0x[0-9a-fA-F]{64}$ ]]; then
            warn "Key doesn't look right (should be 0x + 64 hex chars). Saving anyway."
        fi
        echo "PRIVATE_KEY=$PRIV_KEY" > .env
        ok "Private key saved to .env"
    fi

    # Optional proxy
    echo ""
    echo -e "  ${BOLD}Do you need a proxy?${NC} (for access from restricted regions)"
    echo "  Format: socks5://user:pass@host:port"
    echo -ne "  Proxy URL (press Enter to skip): "
    read -r PROXY
    echo ""

    if [ -n "$PROXY" ]; then
        echo "PROXY_URL=$PROXY" >> .env
        ok "Proxy configured"
    fi

    echo -e "  ${GREEN}━━━ Setup Complete ━━━${NC}"
    echo ""
    echo -e "  To change these later, run: ${BOLD}./setup_wallet.sh${NC}"
    echo ""
fi

# ── Step 6: Launch ─────────────────────────────────────────────
echo -e "  ${BOLD}${GREEN}━━━ Launching PolyArb ━━━${NC}"
echo ""

# Pass through any arguments (e.g., --live, --headless)
exec polyarb "$@"

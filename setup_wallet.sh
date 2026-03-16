#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════╗
# ║  PolyArb — Wallet Setup / Reconfigure                       ║
# ║  Run this to set or change your wallet private key.          ║
# ╚══════════════════════════════════════════════════════════════╝
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo -e "  ${BOLD}PolyArb — Wallet Setup${NC}"
echo ""

if [ -f ".env" ]; then
    warn "A .env file already exists. This will overwrite it."
    echo -ne "  Continue? (y/N): "
    read -r CONFIRM
    if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
        echo "  Cancelled."
        exit 0
    fi
    echo ""
fi

echo -e "  The bot needs your ${BOLD}Polygon wallet private key${NC} to trade."
echo "  This is stored locally in .env and never sent anywhere."
echo ""
echo -e "  ${BOLD}What you need:${NC}"
echo "    1. A Polygon wallet with USDC.e (for trades)"
echo "    2. A small amount of MATIC (for one-time token approval)"
echo "    3. Your wallet's private key (starts with 0x)"
echo ""

echo -ne "  ${BOLD}Paste your private key:${NC} "
read -rs PRIV_KEY
echo ""

if [ -z "$PRIV_KEY" ]; then
    warn "No key entered. Bot will run in dry-run mode only."
    echo "# Wallet not configured — bot runs in dry-run mode only" > .env
    echo "PRIVATE_KEY=" >> .env
else
    if [[ ! "$PRIV_KEY" =~ ^0x[0-9a-fA-F]{64}$ ]]; then
        warn "Key doesn't look right (should be 0x + 64 hex chars). Saving anyway."
    fi
    echo "PRIVATE_KEY=$PRIV_KEY" > .env
    ok "Private key saved"
fi

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

echo -e "  ${GREEN}Done!${NC} Run ${BOLD}./run.sh${NC} to start the bot."
echo ""

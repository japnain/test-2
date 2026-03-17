# PolyArb — Polymarket Arbitrage Bot

Scans Polymarket for mispriced outcomes and executes arbitrage trades automatically.

## Quick Start (Mac/Linux)

Open Terminal and run these 4 commands:

```bash
git clone https://github.com/japnain/test-2.git
cd test-2
git checkout claude/polymarket-arbitrage-bot-n7XSp
./run.sh
```

That's it. The script auto-installs everything and walks you through setup.

## Quick Start (Windows)

Open Command Prompt and run:

```cmd
git clone https://github.com/japnain/test-2.git
cd test-2
git checkout claude/polymarket-arbitrage-bot-n7XSp
run.bat
```

## What You Need

- **Python 3.11+** — [download here](https://www.python.org/downloads/) if you don't have it
- **A Polygon wallet** with USDC.e (for trades) and a small amount of MATIC (for one-time approval)
- **Your wallet's private key** (starts with `0x`) — the script will ask you for it on first run

## Commands

| Command | What it does |
|---|---|
| `./run.sh` | Start the bot in **dry-run mode** (safe, no real trades) |
| `./run.sh --live` | Start the bot in **live mode** (real trades with your funds) |
| `./setup_wallet.sh` | Change your wallet key or proxy settings |

## How It Works

1. Fetches all active Polymarket markets via the CLOB + Gamma APIs
2. Streams real-time price updates over WebSocket
3. Detects arbitrage when outcome prices sum to less than $1.00 (minus fees)
4. In live mode, executes Fill-or-Kill orders on both sides to lock in profit

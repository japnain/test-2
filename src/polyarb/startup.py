"""Startup sequence — branded boot screen with health checks."""

from __future__ import annotations

import asyncio
import logging
import time

from rich.console import Console
from rich.text import Text

from polyarb.client import PolymarketClient
from polyarb.config import Config

logger = logging.getLogger(__name__)

BANNER = r"""
    ____        __      ___         __
   / __ \____  / /_  __/   |  _____/ /_
  / /_/ / __ \/ / / / / /| | / ___/ __ \
 / ____/ /_/ / / /_/ / ___ |/ /  / /_/ /
/_/    \____/_/\__, /_/  |_/_/  /_.___/
              /____/
"""

TAGLINE = "Exploit mispricings, guarantee profits"


async def run_startup(config: Config, client: PolymarketClient, console: Console) -> bool:
    """Run the startup sequence with health checks. Returns True if ready to trade."""

    console.print()
    # Banner
    console.print(Text(BANNER, style="bold cyan"))
    console.print(
        Text(f"         {TAGLINE}", style="dim italic"),
    )
    console.rule(style="cyan")
    console.print()

    # First-time tip
    console.print(
        "[yellow bold]💡 First time running the bot?[/]"
    )
    console.print("   Read the guide: [bold]README.md[/]")
    console.print("   Run health check: [bold]python -m polyarb --headless[/]")
    console.print()

    # Mode
    if config.is_live:
        console.print("[bold red]⚡ MODE: LIVE TRADING[/]")
    else:
        console.print("[bold blue]🔒 MODE: DRY RUN (no real trades)[/]")
    console.print()

    console.rule(style="cyan")

    # Health checks
    console.print()
    console.print("[bold yellow]🏥 HEALTH CHECK[/]")
    console.print()

    all_healthy = True

    # 1. CLOB API
    console.print("  [dim]Checking Polymarket CLOB API...[/]", end="")
    try:
        ok = await client.health_check()
        if ok:
            console.print(" [green]✓[/] API responding")
        else:
            console.print(" [red]✗[/] API not responding")
            all_healthy = False
    except Exception as e:
        console.print(f" [red]✗[/] Error: {e}")
        all_healthy = False

    # 2. Gamma API
    console.print("  [dim]Checking Gamma Markets API...[/]", end="")
    try:
        resp = await client._http.get("/markets", params={"limit": 1})
        if resp.status_code == 200:
            console.print(" [green]✓[/] API responding")
        else:
            console.print(f" [yellow]⚠[/] Status {resp.status_code}")
    except Exception as e:
        console.print(f" [red]✗[/] Error: {e}")
        all_healthy = False

    # 3. Wallet
    wallet_addr = client.get_wallet_address()
    if wallet_addr:
        masked = wallet_addr[:6] + "*" * 30 + wallet_addr[-4:]
        console.print(f"  [dim]Wallet:[/]     [green]✓[/] {masked}")
    else:
        console.print("  [dim]Wallet:[/]     [yellow]⚠[/] No private key configured")

    # 4. Balance
    if wallet_addr:
        console.print("  [dim]Checking balance...[/]", end="")
        balance = await client.get_wallet_balance()
        if balance is not None:
            if balance < 5.0:
                console.print(f" [yellow]⚠[/] Low balance: ${balance:.2f}")
            else:
                console.print(f" [green]✓[/] Balance: ${balance:.2f}")
        else:
            console.print(" [dim]Could not fetch balance[/]")

    # 5. CLOB Client
    console.print("  [dim]Initializing CLOB client...[/]", end="")
    try:
        clob = client._get_clob()
        if client._authenticated:
            console.print(" [green]✓[/] Authenticated (EOA)")
        else:
            console.print(" [green]✓[/] Read-only mode")
    except Exception as e:
        console.print(f" [red]✗[/] Failed: {e}")
        all_healthy = False

    # 6. WebSocket
    if config.scanning.use_websocket:
        console.print("  [dim]WebSocket:[/]  [green]✓[/] Enabled (real-time streaming)")
    else:
        console.print("  [dim]WebSocket:[/]  [dim]Disabled (HTTP polling)[/]")

    console.print()
    console.rule(style="cyan")
    console.print()

    # Overall status
    if all_healthy:
        console.print(
            "  [bold green]Overall Status: ✅ Healthy[/]"
        )
    else:
        console.print(
            "  [bold yellow]Overall Status: ⚠️ Degraded (some checks failed)[/]"
        )

    console.print()

    # Config summary
    console.print("[bold]📊 Configuration:[/]")
    console.print(f"   Strategies:     {', '.join(config.arbitrage.strategies)}")
    console.print(f"   Min profit:     {config.arbitrage.min_profit_bps} bps (${config.arbitrage.min_profit_usd})")
    console.print(f"   Max order:      ${config.execution.max_order_usd}")
    console.print(f"   Max position:   ${config.risk.max_position_usd}")
    console.print(f"   Daily loss cap: ${config.risk.max_daily_loss_usd}")
    if config.proxy_url:
        console.print(f"   Proxy:          {config.proxy_url[:25]}...")
    console.print()

    console.rule(style="cyan")
    console.print()
    console.print("[bold]🚀 Starting trade monitor...[/]")
    console.print()

    return all_healthy

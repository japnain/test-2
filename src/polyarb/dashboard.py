"""Rich terminal dashboard — polished PolyCopy-style live display."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from polyarb.config import Config
from polyarb.risk import RiskManager
from polyarb.scanner import MarketScanner
from polyarb.tracker import PnLTracker

BANNER = r"""
    ____        __      ___         __
   / __ \____  / /_  __/   |  _____/ /_
  / /_/ / __ \/ / / / / /| | / ___/ __ \
 / ____/ /_/ / / /_/ / ___ |/ /  / /_/ /
/_/    \____/_/\__, /_/  |_/_/  /_.___/
              /____/
"""


class Dashboard:
    """Live terminal dashboard — PolyCopy-style vertical flow."""

    def __init__(
        self,
        config: Config,
        scanner: MarketScanner,
        tracker: PnLTracker,
        risk: RiskManager,
        ws_client=None,
    ):
        self.config = config
        self.scanner = scanner
        self.tracker = tracker
        self.risk = risk
        self.ws_client = ws_client
        self.console = Console()
        self._start_time = time.time()

    def _uptime_str(self) -> str:
        uptime = int(time.time() - self._start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def render(self) -> Group:
        """Build the full dashboard as a vertical flow (PolyCopy style)."""
        parts = []

        # ─── BANNER ───
        banner_text = Text(BANNER, style="bold cyan")
        tagline = Text("         Exploit mispricings, guarantee profits", style="dim italic")
        parts.append(banner_text)
        parts.append(tagline)
        parts.append(Text(""))

        # ─── MODE BAR ───
        if self.config.is_live:
            mode = Text("  ⚡ LIVE TRADING  ", style="bold white on red")
        else:
            mode = Text("  🔒 DRY RUN  ", style="bold white on blue")

        status_line = Text()
        status_line.append_text(mode)
        status_line.append(f"  │  Uptime: {self._uptime_str()}")
        status_line.append(f"  │  {datetime.now().strftime('%H:%M:%S')}")
        parts.append(status_line)
        parts.append(Text("━" * 60, style="cyan"))

        # ─── SCANNER STATUS ───
        ws_info = ""
        if self.ws_client:
            if self.ws_client.is_connected:
                latency = self.ws_client.latency_ms
                ws_info = f" [green]●[/] WS connected ({latency:.0f}ms)"
            else:
                ws_info = " [red]●[/] WS disconnected (polling fallback)"
        else:
            ws_info = " [dim]HTTP polling[/]"

        scanner_text = Text.from_markup(
            f"[bold]📡 Scanner:[/]  "
            f"Scans: [bold]{self.scanner.scan_count}[/]  │  "
            f"NegRisk: [bold cyan]{len(self.scanner.neg_risk_events)}[/] events  │  "
            f"Binary: [bold]{len(self.scanner.binary_events)}[/]  │  "
            f"Tokens: [bold]{self.scanner.total_tokens_tracked}[/]  │"
            f"{ws_info}"
        )
        parts.append(scanner_text)
        parts.append(Text(""))

        # ─── TRACKING MARKETS ───
        if self.scanner.neg_risk_events:
            parts.append(Text.from_markup("[bold yellow]📊 Tracking Markets:[/]"))
            shown = self.scanner.neg_risk_events[:8]
            for i, event in enumerate(shown, 1):
                title = event.title[:55] if len(event.title) > 55 else event.title
                parts.append(Text.from_markup(
                    f"   {i}. [dim]{title}[/] "
                    f"[cyan]({event.num_outcomes} outcomes)[/]"
                ))
            if len(self.scanner.neg_risk_events) > 8:
                remaining = len(self.scanner.neg_risk_events) - 8
                parts.append(Text.from_markup(f"   [dim]... and {remaining} more[/]"))
            parts.append(Text(""))

        # ─── OPPORTUNITIES ───
        parts.append(Text("━" * 60, style="yellow"))

        opp_table = Table(
            show_header=True,
            header_style="bold yellow",
            box=None,
            padding=(0, 1),
            expand=True,
        )
        opp_table.add_column("Time", style="dim", width=8)
        opp_table.add_column("Event", max_width=35, no_wrap=True)
        opp_table.add_column("#", width=3, justify="right")
        opp_table.add_column("Cost", width=7, justify="right")
        opp_table.add_column("Profit", width=8, justify="right", style="green")
        opp_table.add_column("USD", width=8, justify="right", style="bold green")
        opp_table.add_column("Status", width=8, justify="center")

        recent = self.tracker.opportunities_log[-8:]
        for opp in reversed(recent):
            ts = datetime.fromtimestamp(opp["timestamp"]).strftime("%H:%M:%S")
            title = opp["event_title"][:35]
            n = str(len(opp["outcomes"]))
            cost = f"${opp['total_cost']:.3f}"
            profit = f"{opp['guaranteed_profit']:.4f}"
            usd = f"${opp['estimated_profit_usd']:.2f}"
            opp_table.add_row(ts, title, n, cost, profit, usd, "🔍")

        if not recent:
            opp_table.add_row(
                "--", "Scanning for opportunities...", "--", "--", "--", "--", "⏳"
            )

        total_opps = self.tracker.total_opportunities_detected
        parts.append(Text.from_markup(
            f"[bold yellow]💰 Opportunities[/] "
            f"[dim]({total_opps} detected)[/]"
        ))
        parts.append(opp_table)
        parts.append(Text(""))

        # ─── P&L + RISK (side by side feel, but vertical) ───
        parts.append(Text("━" * 60, style="green"))

        summary = self.tracker.summary
        risk_s = self.risk.summary

        pnl_line = Text.from_markup(
            f"[bold green]💵 P&L:[/]  "
            f"Trades: [bold]{summary['total_trades']}[/]  │  "
            f"Won: [green]{summary['successful']}[/]  │  "
            f"Failed: [red]{summary['failed']}[/]  │  "
            f"Win Rate: [bold]{summary['win_rate']}[/]  │  "
            f"Profit: [bold green]{summary['expected_profit']}[/]"
        )
        parts.append(pnl_line)

        risk_parts = []
        risk_parts.append(f"Positions: [bold]{risk_s['open_positions']}[/]")
        risk_parts.append(f"Daily P&L: [green]+${risk_s['daily_profit_usd']:.2f}[/]")
        risk_parts.append(f"Daily Loss: [red]-${risk_s['daily_loss_usd']:.2f}[/]")
        if risk_s["in_cooldown"]:
            risk_parts.append("[bold red]COOLDOWN[/]")
        if risk_s["kill_switch"]:
            risk_parts.append("[bold red]KILL SWITCH ON[/]")

        risk_line = Text.from_markup(
            f"[bold red]🛡️  Risk:[/]  " + "  │  ".join(risk_parts)
        )
        parts.append(risk_line)
        parts.append(Text(""))
        parts.append(Text("━" * 60, style="cyan"))

        # ─── FOOTER ───
        footer = Text.from_markup(
            f"[dim]Press Ctrl+C to stop  │  "
            f"Config: config.yaml  │  "
            f"Logs: {self.config.logging.trade_log}[/]"
        )
        parts.append(footer)

        return Group(*parts)

    async def run_loop(self):
        """Run the dashboard with live updates."""
        with Live(
            self.render(),
            console=self.console,
            refresh_per_second=2,
            screen=True,
        ) as live:
            while True:
                try:
                    live.update(self.render())
                    await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    break

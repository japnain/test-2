"""Rich terminal dashboard — live display of bot status."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from polyarb.config import Config
from polyarb.risk import RiskManager
from polyarb.scanner import MarketScanner
from polyarb.tracker import PnLTracker


class Dashboard:
    """Live terminal dashboard using Rich."""

    def __init__(
        self,
        config: Config,
        scanner: MarketScanner,
        tracker: PnLTracker,
        risk: RiskManager,
    ):
        self.config = config
        self.scanner = scanner
        self.tracker = tracker
        self.risk = risk
        self.console = Console()
        self._start_time = time.time()

    def _build_header(self) -> Panel:
        """Build the header with mode indicator."""
        if self.config.is_live:
            mode_text = Text(" MODE: LIVE ", style="bold white on red")
        else:
            mode_text = Text(" MODE: DRY RUN ", style="bold white on blue")

        uptime = int(time.time() - self._start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)

        header = Text()
        header.append("POLYMARKET ARBITRAGE BOT", style="bold cyan")
        header.append("  |  ")
        header.append_text(mode_text)
        header.append(f"  |  Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}")
        header.append(f"  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        return Panel(header, style="bold")

    def _build_scanner_info(self) -> Panel:
        """Build scanner status panel."""
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Label", style="dim")
        table.add_column("Value", style="bold")

        table.add_row("Scans", str(self.scanner.scan_count))
        table.add_row("NegRisk Events", str(len(self.scanner.neg_risk_events)))
        table.add_row("Binary Markets", str(len(self.scanner.binary_events)))
        table.add_row("Total Tokens", str(self.scanner.total_tokens_tracked))
        table.add_row(
            "Poll Interval", f"{self.config.scanning.poll_interval_sec}s"
        )

        return Panel(table, title="Scanner", border_style="green")

    def _build_opportunities(self) -> Panel:
        """Build recent opportunities panel."""
        table = Table(box=None)
        table.add_column("Time", style="dim", width=8)
        table.add_column("Event", max_width=40)
        table.add_column("Outcomes", width=5, justify="right")
        table.add_column("Cost", width=8, justify="right")
        table.add_column("Profit", width=10, justify="right", style="green")
        table.add_column("USD", width=8, justify="right", style="bold green")

        # Show last 10 opportunities
        recent = self.tracker.opportunities_log[-10:]
        for opp in reversed(recent):
            ts = datetime.fromtimestamp(opp["timestamp"]).strftime("%H:%M:%S")
            title = opp["event_title"][:40]
            n_outcomes = str(len(opp["outcomes"]))
            cost = f"${opp['total_cost']:.4f}"
            profit = f"{opp['guaranteed_profit']:.4f}"
            usd = f"${opp['estimated_profit_usd']:.2f}"
            table.add_row(ts, title, n_outcomes, cost, profit, usd)

        if not recent:
            table.add_row("--", "No opportunities detected yet", "--", "--", "--", "--")

        return Panel(
            table,
            title=f"Recent Opportunities ({self.tracker.total_opportunities_detected} total)",
            border_style="yellow",
        )

    def _build_pnl(self) -> Panel:
        """Build P&L summary panel."""
        summary = self.tracker.summary
        risk_summary = self.risk.summary

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Label", style="dim")
        table.add_column("Value", style="bold")

        table.add_row("Total Trades", str(summary["total_trades"]))
        table.add_row("Successful", str(summary["successful"]))
        table.add_row("Failed", str(summary["failed"]))
        table.add_row("Dry Run", str(summary["dry_run"]))
        table.add_row("Win Rate", summary["win_rate"])
        table.add_row("Total Invested", summary["total_invested"])
        table.add_row(
            "Expected Profit",
            Text(summary["expected_profit"], style="bold green"),
        )

        return Panel(table, title="P&L", border_style="cyan")

    def _build_risk(self) -> Panel:
        """Build risk status panel."""
        summary = self.risk.summary

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Label", style="dim")
        table.add_column("Value", style="bold")

        table.add_row("Open Positions", str(summary["open_positions"]))
        table.add_row("Daily Profit", f"${summary['daily_profit_usd']:.2f}")
        table.add_row("Daily Loss", f"${summary['daily_loss_usd']:.2f}")
        table.add_row(
            "Cooldown",
            Text("YES", style="bold red") if summary["in_cooldown"] else "No",
        )
        table.add_row(
            "Kill Switch",
            Text("ON", style="bold red") if summary["kill_switch"] else "Off",
        )

        return Panel(table, title="Risk", border_style="red")

    def render(self) -> Layout:
        """Build the full dashboard layout."""
        layout = Layout()

        layout.split_column(
            Layout(self._build_header(), size=3),
            Layout(name="top", size=10),
            Layout(self._build_opportunities(), name="middle"),
        )

        layout["top"].split_row(
            Layout(self._build_scanner_info()),
            Layout(self._build_pnl()),
            Layout(self._build_risk()),
        )

        return layout

    async def run_loop(self):
        """Run the dashboard with live updates."""
        with Live(
            self.render(),
            console=self.console,
            refresh_per_second=1,
            screen=True,
        ) as live:
            while True:
                try:
                    live.update(self.render())
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    break

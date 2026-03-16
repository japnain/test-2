"""Main entry point — async orchestrator for the Polymarket arbitrage bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from rich.console import Console

from polyarb.client import PolymarketClient
from polyarb.config import load_config
from polyarb.dashboard import Dashboard
from polyarb.detector import ArbDetector
from polyarb.executor import Executor
from polyarb.models import ArbOpportunity, EventMarket, OrderBookSnapshot
from polyarb.risk import RiskManager
from polyarb.scanner import MarketScanner
from polyarb.startup import run_startup
from polyarb.tracker import PnLTracker

logger = logging.getLogger("polyarb")


def setup_logging(level: str = "INFO"):
    """Configure structured logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


class ArbBot:
    """Main bot orchestrator."""

    def __init__(self, config_path: str = "config.yaml", dry_run_override: bool = False):
        self.config = load_config(config_path)
        if dry_run_override:
            self.config.mode = "dry_run"

        setup_logging(self.config.logging.level)

        self.console = Console()
        self.client = PolymarketClient(self.config)
        self.scanner = MarketScanner(self.client, self.config)
        self.detector = ArbDetector(self.config)
        self.executor = Executor(self.client, self.config)
        self.risk = RiskManager(self.config)
        self.tracker = PnLTracker(self.config)

        # WebSocket components (initialized if enabled)
        self.ws_client = None
        self.ws_mirror = None

        self.dashboard = Dashboard(
            self.config, self.scanner, self.tracker, self.risk
        )

    async def _init_websocket(self):
        """Initialize WebSocket client if enabled."""
        if not self.config.scanning.use_websocket:
            return

        try:
            from polyarb.ws_client import WebSocketClient, OrderBookMirror

            self.ws_mirror = OrderBookMirror()
            self.ws_client = WebSocketClient(
                self.config, self.ws_mirror
            )
            self.scanner.set_ws_client(self.ws_client, self.ws_mirror)
            self.dashboard.ws_client = self.ws_client
            logger.info("WebSocket client initialized")
        except ImportError as e:
            logger.warning(f"WebSocket not available (install websockets): {e}")
        except Exception as e:
            logger.warning(f"WebSocket init failed, falling back to polling: {e}")

    async def process_scan(
        self,
        events_with_books: list[tuple[EventMarket, dict[str, OrderBookSnapshot]]],
    ):
        """Process scan results: detect opportunities and execute trades."""
        opportunities = self.detector.evaluate(events_with_books)

        for opp in opportunities:
            self.tracker.record_opportunity(opp)

            allowed, reason = self.risk.pre_check(opp)
            if not allowed:
                logger.debug(f"Risk rejected: {reason} — {opp.event_title}")
                continue

            result = await self.executor.execute(opp)
            self.tracker.record_trade(result)
            self.risk.record_trade(result)

    async def run(self):
        """Main run loop with startup sequence and dashboard."""
        # Run startup sequence
        await run_startup(self.config, self.client, self.console)

        # Initialize WebSocket
        await self._init_websocket()

        # Run everything concurrently
        try:
            tasks = []
            async with asyncio.TaskGroup() as tg:
                # WebSocket connection (if enabled)
                if self.ws_client:
                    tasks.append(tg.create_task(self.ws_client.connect()))

                # Scanner loop (uses WS mirror if available, falls back to HTTP)
                if self.ws_client and self.ws_mirror:
                    tasks.append(
                        tg.create_task(
                            self.scanner.run_ws_event_loop(
                                on_scan=self.process_scan
                            )
                        )
                    )
                # Always run HTTP polling as well (catches anything WS misses)
                tasks.append(
                    tg.create_task(
                        self.scanner.run_loop(on_scan=self.process_scan)
                    )
                )

                # Dashboard
                tasks.append(tg.create_task(self.dashboard.run_loop()))

        except* asyncio.CancelledError:
            logger.info("Bot shutting down...")
        except* KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            await self.shutdown()

    async def run_headless(self):
        """Run without dashboard (logging-only mode)."""
        # Run startup sequence
        await run_startup(self.config, self.client, self.console)

        # Initialize WebSocket
        await self._init_websocket()

        try:
            tasks = []
            async with asyncio.TaskGroup() as tg:
                if self.ws_client:
                    tg.create_task(self.ws_client.connect())
                if self.ws_client and self.ws_mirror:
                    tg.create_task(
                        self.scanner.run_ws_event_loop(on_scan=self.process_scan)
                    )
                tg.create_task(self.scanner.run_loop(on_scan=self.process_scan))
        except* asyncio.CancelledError:
            pass
        except* KeyboardInterrupt:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self):
        """Clean shutdown."""
        logger.info("Shutting down...")
        logger.info(f"Final stats: {self.tracker.summary}")
        if self.ws_client:
            await self.ws_client.stop()
        await self.client.close()
        logger.info("Bot stopped.")


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="PolyArb — Polymarket Arbitrage Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  polyarb                    Run with default config (dry_run mode)
  polyarb --live             Run in LIVE mode (real trades!)
  polyarb --headless         Run without terminal dashboard
  polyarb --config alt.yaml  Use alternate config file
  polyarb --no-ws            Disable WebSocket (HTTP polling only)
        """,
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to config YAML file (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Force dry-run mode (overrides config)",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Force live mode (REAL TRADES!)",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run without terminal dashboard (logging only)",
    )
    parser.add_argument(
        "--no-ws", action="store_true",
        help="Disable WebSocket streaming (HTTP polling only)",
    )

    args = parser.parse_args()

    bot = ArbBot(
        config_path=args.config,
        dry_run_override=args.dry_run,
    )

    if args.live:
        bot.config.mode = "live"

    if args.no_ws:
        bot.config.scanning.use_websocket = False

    # Handle signals for graceful shutdown
    loop = asyncio.new_event_loop()

    def signal_handler(sig, frame):
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        if args.headless:
            loop.run_until_complete(bot.run_headless())
        else:
            loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()

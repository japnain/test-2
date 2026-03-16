"""Main entry point — async orchestrator for the Polymarket arbitrage bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from polyarb.client import PolymarketClient
from polyarb.config import load_config
from polyarb.dashboard import Dashboard
from polyarb.detector import ArbDetector
from polyarb.executor import Executor
from polyarb.models import ArbOpportunity, EventMarket, OrderBookSnapshot
from polyarb.risk import RiskManager
from polyarb.scanner import MarketScanner
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


class ArbBot:
    """Main bot orchestrator."""

    def __init__(self, config_path: str = "config.yaml", dry_run_override: bool = False):
        self.config = load_config(config_path)
        if dry_run_override:
            self.config.mode = "dry_run"

        setup_logging(self.config.logging.level)

        self.client = PolymarketClient(self.config)
        self.scanner = MarketScanner(self.client, self.config)
        self.detector = ArbDetector(self.config)
        self.executor = Executor(self.client, self.config)
        self.risk = RiskManager(self.config)
        self.tracker = PnLTracker(self.config)
        self.dashboard = Dashboard(
            self.config, self.scanner, self.tracker, self.risk
        )

        self._running = True

    async def process_scan(
        self,
        events_with_books: list[tuple[EventMarket, dict[str, OrderBookSnapshot]]],
    ):
        """Process scan results: detect opportunities and execute trades."""
        # Detect opportunities
        opportunities = self.detector.evaluate(events_with_books)

        for opp in opportunities:
            # Log every opportunity
            self.tracker.record_opportunity(opp)

            # Risk check
            allowed, reason = self.risk.pre_check(opp)
            if not allowed:
                logger.debug(f"Risk rejected: {reason} — {opp.event_title}")
                continue

            # Execute
            result = await self.executor.execute(opp)

            # Record
            self.tracker.record_trade(result)
            self.risk.record_trade(result)

    async def run(self):
        """Main run loop."""
        logger.info("=" * 60)
        logger.info("POLYMARKET ARBITRAGE BOT STARTING")
        logger.info(f"Mode: {'LIVE' if self.config.is_live else 'DRY RUN'}")
        logger.info(f"Strategies: {self.config.arbitrage.strategies}")
        logger.info(f"Min profit: {self.config.arbitrage.min_profit_bps} bps")
        logger.info(f"Poll interval: {self.config.scanning.poll_interval_sec}s")
        if self.config.proxy_url:
            logger.info(f"Proxy: {self.config.proxy_url[:20]}...")
        logger.info("=" * 60)

        # Health check
        healthy = await self.client.health_check()
        if not healthy:
            logger.error("CLOB API health check failed! Check your connection.")
            logger.info("Continuing anyway — the API may become available...")

        # Run scanner and dashboard concurrently
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.scanner.run_loop(on_scan=self.process_scan))
                tg.create_task(self.dashboard.run_loop())
        except* asyncio.CancelledError:
            logger.info("Bot shutting down...")
        except* KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            await self.shutdown()

    async def run_headless(self):
        """Run without dashboard (for logging-only mode)."""
        logger.info("=" * 60)
        logger.info("POLYMARKET ARBITRAGE BOT STARTING (headless)")
        logger.info(f"Mode: {'LIVE' if self.config.is_live else 'DRY RUN'}")
        logger.info("=" * 60)

        healthy = await self.client.health_check()
        if not healthy:
            logger.error("CLOB API health check failed!")

        try:
            await self.scanner.run_loop(on_scan=self.process_scan)
        except asyncio.CancelledError:
            pass
        except KeyboardInterrupt:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self):
        """Clean shutdown."""
        logger.info("Shutting down...")
        logger.info(f"Final stats: {self.tracker.summary}")
        await self.client.close()
        logger.info("Bot stopped.")


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Polymarket Arbitrage Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  polyarb                    Run with default config (dry_run mode)
  polyarb --live             Run in LIVE mode (real trades!)
  polyarb --headless         Run without terminal dashboard
  polyarb --config alt.yaml  Use alternate config file
        """,
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config YAML file (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run mode (overrides config)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Force live mode (overrides config — REAL TRADES!)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without terminal dashboard (logging only)",
    )

    args = parser.parse_args()

    bot = ArbBot(
        config_path=args.config,
        dry_run_override=args.dry_run,
    )

    if args.live:
        bot.config.mode = "live"
        print("\n!!! LIVE MODE ENABLED — REAL TRADES WILL BE PLACED !!!\n")

    # Handle signals for graceful shutdown
    loop = asyncio.new_event_loop()

    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}")
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

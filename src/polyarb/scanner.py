"""Market scanner — discovers markets and polls order books."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Awaitable

from polyarb.client import PolymarketClient
from polyarb.config import Config
from polyarb.models import EventMarket, OrderBookSnapshot

logger = logging.getLogger(__name__)


class MarketScanner:
    """Scans Polymarket for active events and fetches order books."""

    def __init__(self, client: PolymarketClient, config: Config):
        self.client = client
        self.config = config
        self.events: list[EventMarket] = []
        self.neg_risk_events: list[EventMarket] = []
        self.binary_events: list[EventMarket] = []
        self._last_market_refresh = 0.0
        self.scan_count = 0
        self.total_tokens_tracked = 0

    async def refresh_markets(self):
        """Fetch and categorize all active markets."""
        logger.info("Refreshing market list from Gamma API...")
        raw_markets = await self.client.get_active_markets()
        self.events = self.client.parse_markets_into_events(raw_markets)

        # Separate NegRisk (multi-outcome) from binary
        self.neg_risk_events = [
            e for e in self.events if e.neg_risk and e.num_outcomes >= 2
        ]
        self.binary_events = [
            e for e in self.events if not e.neg_risk and e.num_outcomes == 2
        ]

        # Count total tokens to track
        self.total_tokens_tracked = sum(e.num_outcomes for e in self.events)

        self._last_market_refresh = time.time()
        logger.info(
            f"Markets loaded: {len(self.neg_risk_events)} NegRisk events "
            f"({sum(e.num_outcomes for e in self.neg_risk_events)} outcomes), "
            f"{len(self.binary_events)} binary markets"
        )

    async def fetch_event_books(
        self, event: EventMarket
    ) -> dict[str, OrderBookSnapshot]:
        """Fetch order books for all outcomes of an event."""
        token_ids = [o.token_id for o in event.outcomes]
        return await self.client.get_order_books_batch(token_ids)

    async def scan_once(
        self,
    ) -> list[tuple[EventMarket, dict[str, OrderBookSnapshot]]]:
        """Run a single scan cycle. Returns events with their order books."""
        now = time.time()

        # Refresh market list if stale
        if now - self._last_market_refresh > self.config.scanning.market_refresh_sec:
            await self.refresh_markets()

        if not self.events:
            await self.refresh_markets()

        results = []

        # Fetch order books for NegRisk events (primary strategy)
        if "multi_outcome" in self.config.arbitrage.strategies:
            for event in self.neg_risk_events:
                try:
                    books = await self.fetch_event_books(event)
                    results.append((event, books))
                except Exception as e:
                    logger.error(f"Error scanning event {event.event_id}: {e}")

        # Fetch order books for binary events (secondary strategy)
        if "binary" in self.config.arbitrage.strategies:
            for event in self.binary_events:
                try:
                    books = await self.fetch_event_books(event)
                    results.append((event, books))
                except Exception as e:
                    logger.error(f"Error scanning binary {event.event_id}: {e}")

        self.scan_count += 1
        return results

    async def run_loop(
        self,
        on_scan: Callable[
            [list[tuple[EventMarket, dict[str, OrderBookSnapshot]]]],
            Awaitable[None],
        ],
    ):
        """Main scanning loop. Calls on_scan with results each cycle."""
        logger.info(
            f"Scanner starting — polling every {self.config.scanning.poll_interval_sec}s"
        )

        while True:
            try:
                start = time.monotonic()
                results = await self.scan_once()
                await on_scan(results)
                elapsed = time.monotonic() - start

                # Sleep for remaining interval
                sleep_time = max(
                    0, self.config.scanning.poll_interval_sec - elapsed
                )
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                logger.info("Scanner loop cancelled")
                break
            except Exception as e:
                logger.error(f"Scanner error: {e}", exc_info=True)
                await asyncio.sleep(5)  # Back off on error

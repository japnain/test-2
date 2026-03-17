"""Market scanner — discovers markets and polls/streams order books."""

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
    """Scans Polymarket for active events and fetches order books.

    Supports two modes:
    - HTTP polling (fallback): fetches order books every poll_interval_sec
    - WebSocket streaming (primary): receives real-time updates via WS
    """

    def __init__(self, client: PolymarketClient, config: Config):
        self.client = client
        self.config = config
        self.events: list[EventMarket] = []
        self.neg_risk_events: list[EventMarket] = []
        self.binary_events: list[EventMarket] = []
        self._last_market_refresh = 0.0
        self.scan_count = 0
        self.total_tokens_tracked = 0

        # WebSocket integration
        self.ws_client = None
        self.ws_mirror = None
        self._ws_mode = False

        # Latency tracking
        self.last_scan_latency_ms = 0.0

    def set_ws_client(self, ws_client, mirror):
        """Attach a WebSocket client and order book mirror."""
        self.ws_client = ws_client
        self.ws_mirror = mirror
        self._ws_mode = True

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

        # If WS mode, subscribe to all tracked tokens
        if self._ws_mode and self.ws_client:
            all_tokens = []
            for event in self.events:
                for outcome in event.outcomes:
                    all_tokens.append(outcome.token_id)
            await self.ws_client.subscribe(all_tokens)
            logger.info(f"Subscribed to {len(all_tokens)} tokens via WebSocket")

    async def fetch_event_books(
        self, event: EventMarket
    ) -> dict[str, OrderBookSnapshot]:
        """Fetch order books for all outcomes of an event."""
        token_ids = [o.token_id for o in event.outcomes]

        # If WS mirror has data, use it (near-zero latency)
        if self._ws_mode and self.ws_mirror:
            mirror_books = self.ws_mirror.get_all(token_ids)
            # Use mirror data if we have ANY tokens (partial is fine)
            if mirror_books:
                fresh_books = {
                    tid: book for tid, book in mirror_books.items()
                    if not self.ws_mirror.is_stale(tid)
                }
                if fresh_books:
                    return fresh_books

        # Fallback to HTTP fetch
        return await self.client.get_order_books_batch(token_ids)

    async def scan_once(
        self,
    ) -> list[tuple[EventMarket, dict[str, OrderBookSnapshot]]]:
        """Run a single scan cycle. Returns events with their order books.

        Scans a rotating batch of events per cycle (not all at once) to keep
        each cycle fast. Uses concurrent fetches within each batch.
        """
        start = time.monotonic()
        now = time.time()

        # Refresh market list if stale
        if now - self._last_market_refresh > self.config.scanning.market_refresh_sec:
            await self.refresh_markets()

        if not self.events:
            await self.refresh_markets()

        results = []
        batch_size = 50  # Events per scan cycle
        concurrency = 10  # Max concurrent HTTP fetches
        sem = asyncio.Semaphore(concurrency)

        async def _fetch_with_sem(event: EventMarket):
            async with sem:
                return event, await self.fetch_event_books(event)

        # Build list of events to scan this cycle (rotating batch)
        events_to_scan = []

        if "multi_outcome" in self.config.arbitrage.strategies and self.neg_risk_events:
            n = len(self.neg_risk_events)
            start_idx = (self.scan_count * batch_size) % n
            events_to_scan.extend(
                self.neg_risk_events[start_idx:start_idx + batch_size]
            )

        if "binary" in self.config.arbitrage.strategies and self.binary_events:
            n = len(self.binary_events)
            start_idx = (self.scan_count * batch_size) % n
            events_to_scan.extend(
                self.binary_events[start_idx:start_idx + batch_size]
            )

        # Fetch all books concurrently
        if events_to_scan:
            tasks = [_fetch_with_sem(event) for event in events_to_scan]
            completed = await asyncio.gather(*tasks, return_exceptions=True)
            for result in completed:
                if isinstance(result, Exception):
                    logger.error(f"Error scanning event: {result}")
                else:
                    event, books = result
                    if books:
                        results.append((event, books))

        self.scan_count += 1
        self.last_scan_latency_ms = (time.monotonic() - start) * 1000
        return results

    async def run_loop(
        self,
        on_scan: Callable[
            [list[tuple[EventMarket, dict[str, OrderBookSnapshot]]]],
            Awaitable[None],
        ],
    ):
        """Main scanning loop. Calls on_scan with results each cycle."""
        mode = "WebSocket + polling" if self._ws_mode else "HTTP polling"
        logger.info(
            f"Scanner starting — {mode}, "
            f"poll every {self.config.scanning.poll_interval_sec}s"
        )

        while True:
            try:
                results = await self.scan_once()
                await on_scan(results)

                # Sleep for remaining interval
                await asyncio.sleep(self.config.scanning.poll_interval_sec)
            except asyncio.CancelledError:
                logger.info("Scanner loop cancelled")
                break
            except Exception as e:
                logger.error(f"Scanner error: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def run_ws_event_loop(
        self,
        on_scan: Callable[
            [list[tuple[EventMarket, dict[str, OrderBookSnapshot]]]],
            Awaitable[None],
        ],
    ):
        """WebSocket-driven event loop — triggers arb detection on every update.

        This provides near-zero latency: instead of polling every 1s, we
        re-evaluate opportunities whenever any order book changes.
        """
        if not self._ws_mode or not self.ws_client:
            logger.warning("WS event loop called without WS client, falling back to polling")
            await self.run_loop(on_scan)
            return

        logger.info("WS event loop starting — detecting arbs on every order book update")

        # Debounce: batch updates that arrive within 50ms
        pending_tokens: set[str] = set()
        last_process = time.monotonic()
        debounce_sec = 0.05  # 50ms debounce

        async def on_ws_update(token_id: str):
            nonlocal last_process
            pending_tokens.add(token_id)
            now = time.monotonic()
            if now - last_process < debounce_sec:
                return
            last_process = now

            # Find events affected by this token update
            affected_events = []
            for event in self.events:
                for outcome in event.outcomes:
                    if outcome.token_id in pending_tokens:
                        affected_events.append(event)
                        break

            if not affected_events:
                pending_tokens.clear()
                return

            # Build results from WS mirror
            results = []
            for event in affected_events:
                token_ids = [o.token_id for o in event.outcomes]
                books = self.ws_mirror.get_all(token_ids)
                if len(books) == len(token_ids):
                    results.append((event, books))

            if results:
                self.scan_count += 1
                await on_scan(results)

            pending_tokens.clear()

        # Set the callback on the WS client
        self.ws_client.on_update = on_ws_update

        # Keep running (the WS client drives events)
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("WS event loop cancelled")

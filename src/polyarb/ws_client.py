"""WebSocket client for real-time Polymarket order book streaming.

Connects to Polymarket's CLOB WebSocket API for near-zero latency
order book updates. Falls back to HTTP polling if WS fails.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Awaitable

import websockets
from websockets.asyncio.client import connect as ws_connect

from polyarb.config import Config
from polyarb.models import OrderBookSnapshot, PriceLevel

logger = logging.getLogger(__name__)


class OrderBookMirror:
    """Maintains a local mirror of order books from WebSocket updates."""

    def __init__(self):
        self._books: dict[str, OrderBookSnapshot] = {}
        self._last_update: dict[str, float] = {}

    def get(self, token_id: str) -> OrderBookSnapshot | None:
        return self._books.get(token_id)

    def get_all(self, token_ids: list[str]) -> dict[str, OrderBookSnapshot]:
        return {tid: self._books[tid] for tid in token_ids if tid in self._books}

    def update_book(self, token_id: str, book: OrderBookSnapshot):
        self._books[token_id] = book
        self._last_update[token_id] = time.time()

    def apply_delta(self, token_id: str, changes: list[dict]):
        """Apply incremental order book changes."""
        book = self._books.get(token_id)
        if not book:
            return

        for change in changes:
            side = change.get("side", "")
            price = float(change.get("price", 0))
            size = float(change.get("size", 0))

            if side == "sell" or side == "ask":
                levels = book.asks
            elif side == "buy" or side == "bid":
                levels = book.bids
            else:
                continue

            # Find and update or remove the level
            found = False
            for i, level in enumerate(levels):
                if abs(level.price - price) < 1e-10:
                    if size <= 0:
                        levels.pop(i)
                    else:
                        levels[i] = PriceLevel(price=price, size=size)
                    found = True
                    break

            if not found and size > 0:
                levels.append(PriceLevel(price=price, size=size))

            # Re-sort
            if side in ("sell", "ask"):
                levels.sort(key=lambda x: x.price)
            else:
                levels.sort(key=lambda x: x.price, reverse=True)

        book.timestamp = time.time()
        self._last_update[token_id] = time.time()

    def is_stale(self, token_id: str, max_age_sec: float = 30.0) -> bool:
        last = self._last_update.get(token_id, 0)
        return (time.time() - last) > max_age_sec

    @property
    def tracked_count(self) -> int:
        return len(self._books)


class WebSocketClient:
    """Real-time WebSocket client for Polymarket order book streaming."""

    def __init__(
        self,
        config: Config,
        mirror: OrderBookMirror,
        on_update: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.config = config
        self.mirror = mirror
        self.on_update = on_update
        self._ws = None
        self._connected = False
        self._subscribed_tokens: set[str] = set()
        self._reconnect_delay = config.scanning.ws_reconnect_delay_sec
        self._max_reconnect_delay = 30.0
        self._running = False

        # Metrics
        self.messages_received = 0
        self.last_message_time = 0.0
        self.connection_count = 0
        self.latency_ms = 0.0

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self):
        """Connect to WebSocket and start receiving."""
        self._running = True
        delay = self._reconnect_delay

        while self._running:
            try:
                logger.info(f"Connecting to WebSocket: {self.config.scanning.ws_url}")
                async with ws_connect(
                    self.config.scanning.ws_url,
                    ping_interval=10,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self.connection_count += 1
                    delay = self._reconnect_delay  # Reset delay on success
                    logger.info("WebSocket connected")

                    # Re-subscribe to all tracked tokens
                    for token_id in self._subscribed_tokens:
                        await self._send_subscribe(token_id)

                    # Process messages
                    async for raw_msg in ws:
                        if not self._running:
                            break
                        await self._handle_message(raw_msg)

            except websockets.ConnectionClosed as e:
                logger.warning(f"WebSocket closed: {e}")
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
            finally:
                self._connected = False
                self._ws = None

            if not self._running:
                break

            # Reconnect with exponential backoff
            logger.info(f"Reconnecting in {delay:.1f}s...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_reconnect_delay)

    async def subscribe(self, token_ids: list[str]):
        """Subscribe to order book updates for given tokens."""
        new_tokens = set(token_ids) - self._subscribed_tokens
        self._subscribed_tokens.update(token_ids)

        if self._connected and self._ws:
            for token_id in new_tokens:
                await self._send_subscribe(token_id)

    async def unsubscribe(self, token_ids: list[str]):
        """Unsubscribe from token updates."""
        for token_id in token_ids:
            self._subscribed_tokens.discard(token_id)
            if self._connected and self._ws:
                try:
                    msg = json.dumps({
                        "type": "unsubscribe",
                        "channel": "market",
                        "assets_id": token_id,
                    })
                    await self._ws.send(msg)
                except Exception:
                    pass

    async def stop(self):
        """Stop the WebSocket client."""
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _send_subscribe(self, token_id: str):
        """Send a subscribe message for a token."""
        if not self._ws:
            return
        try:
            msg = json.dumps({
                "type": "subscribe",
                "channel": "market",
                "assets_id": token_id,
            })
            await self._ws.send(msg)
            logger.debug(f"Subscribed to {token_id[:16]}...")
        except Exception as e:
            logger.error(f"Failed to subscribe {token_id[:16]}: {e}")

    async def _handle_message(self, raw_msg: str | bytes):
        """Process an incoming WebSocket message."""
        recv_time = time.time()
        self.messages_received += 1
        self.last_message_time = recv_time

        try:
            if isinstance(raw_msg, bytes):
                raw_msg = raw_msg.decode("utf-8")

            data = json.loads(raw_msg)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.debug(f"Failed to parse WS message: {e}")
            return

        msg_type = data.get("event_type") or data.get("type", "")
        channel = data.get("channel", "")

        if msg_type in ("book", "book_snapshot"):
            # Full book snapshot
            asset_id = data.get("asset_id", "")
            if asset_id:
                book = self._parse_book_snapshot(asset_id, data)
                self.mirror.update_book(asset_id, book)
                if self.on_update:
                    await self.on_update(asset_id)

        elif msg_type in ("price_change", "book_update", "book_delta"):
            # Incremental update
            asset_id = data.get("asset_id", "")
            changes = data.get("changes", data.get("updates", []))
            if asset_id and changes:
                self.mirror.apply_delta(asset_id, changes)
                if self.on_update:
                    await self.on_update(asset_id)

        elif msg_type in ("last_trade_price", "trade"):
            # Trade event — can trigger a book refresh
            asset_id = data.get("asset_id", "")
            if asset_id and self.on_update:
                await self.on_update(asset_id)

        # Calculate latency if timestamp available
        server_ts = data.get("timestamp")
        if server_ts:
            try:
                if isinstance(server_ts, (int, float)):
                    if server_ts > 1e12:  # milliseconds
                        server_ts = server_ts / 1000
                    self.latency_ms = (recv_time - server_ts) * 1000
            except (ValueError, TypeError):
                pass

    def _parse_book_snapshot(self, asset_id: str, data: dict) -> OrderBookSnapshot:
        """Parse a full book snapshot from WebSocket."""
        asks = []
        bids = []

        for ask in data.get("asks", data.get("sell", [])):
            price = float(ask.get("price", ask.get("p", 0)))
            size = float(ask.get("size", ask.get("s", 0)))
            if price > 0 and size > 0:
                asks.append(PriceLevel(price=price, size=size))

        for bid in data.get("bids", data.get("buy", [])):
            price = float(bid.get("price", bid.get("p", 0)))
            size = float(bid.get("size", bid.get("s", 0)))
            if price > 0 and size > 0:
                bids.append(PriceLevel(price=price, size=size))

        asks.sort(key=lambda x: x.price)
        bids.sort(key=lambda x: x.price, reverse=True)

        return OrderBookSnapshot(
            token_id=asset_id,
            asks=asks,
            bids=bids,
            timestamp=time.time(),
        )

"""API client wrapper for Polymarket CLOB and Gamma APIs."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams, OrderArgs, MarketOrderArgs, OrderType
from py_clob_client.constants import POLYGON

from polyarb.config import Config
from polyarb.models import (
    EventMarket,
    MarketOutcome,
    OrderBookSnapshot,
    PriceLevel,
)

logger = logging.getLogger(__name__)


class RateLimiter:
    """Token bucket rate limiter."""

    def __init__(self, max_requests: int, per_seconds: float):
        self.max_requests = max_requests
        self.per_seconds = per_seconds
        self.tokens = max_requests
        self.last_refill = time.monotonic()

    async def acquire(self):
        while True:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(
                self.max_requests,
                self.tokens + elapsed * (self.max_requests / self.per_seconds),
            )
            self.last_refill = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            await asyncio.sleep(0.01)


class PolymarketClient:
    """Unified client for Polymarket CLOB and Gamma APIs."""

    def __init__(self, config: Config):
        self.config = config
        self._clob: ClobClient | None = None
        self._authenticated = False

        # HTTP client for Gamma API (async, with optional proxy)
        transport_kwargs = {}
        if config.proxy_url:
            transport_kwargs["proxy"] = config.proxy_url

        self._http = httpx.AsyncClient(
            base_url=config.gamma_url,
            timeout=30.0,
            **transport_kwargs,
        )

        # Rate limiters
        self._public_limiter = RateLimiter(max_requests=200, per_seconds=60)
        self._order_limiter = RateLimiter(max_requests=50, per_seconds=60)

        # Fee rate cache
        self._cached_fee_rate: float | None = None
        self._fee_rate_fetched_at: float = 0.0
        self._fee_rate_ttl: float = 300.0  # 5 minutes

    def _get_clob(self) -> ClobClient:
        """Lazy-init the CLOB client."""
        if self._clob is None:
            if self.config.private_key and self.config.is_live:
                self._clob = ClobClient(
                    self.config.clob_url,
                    key=self.config.private_key,
                    chain_id=self.config.chain_id,
                )
                # Derive API credentials
                self._clob.set_api_creds(self._clob.create_or_derive_api_creds())
                self._authenticated = True
                logger.info("CLOB client initialized with authentication")
            else:
                self._clob = ClobClient(self.config.clob_url)
                logger.info("CLOB client initialized in read-only mode")
        return self._clob

    async def health_check(self) -> bool:
        """Check if CLOB API is reachable."""
        try:
            clob = self._get_clob()
            result = await asyncio.to_thread(clob.get_ok)
            return result == "OK"
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    # -------------------------------------------------------------------------
    # Gamma API — Market Discovery
    # -------------------------------------------------------------------------

    async def get_active_markets(self) -> list[dict]:
        """Fetch all active markets from Gamma API."""
        await self._public_limiter.acquire()
        markets = []
        offset = 0
        limit = 100

        while True:
            await self._public_limiter.acquire()
            try:
                resp = await self._http.get(
                    "/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": limit,
                        "offset": offset,
                    },
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                markets.extend(batch)
                if len(batch) < limit:
                    break
                offset += limit
                if len(markets) >= self.config.scanning.max_markets:
                    break
            except Exception as e:
                logger.error(f"Failed to fetch markets at offset {offset}: {e}")
                break

        logger.info(f"Fetched {len(markets)} active markets from Gamma API")
        return markets

    async def get_events(self) -> list[dict]:
        """Fetch active events from Gamma API."""
        await self._public_limiter.acquire()
        events = []
        offset = 0
        limit = 100

        while True:
            await self._public_limiter.acquire()
            try:
                resp = await self._http.get(
                    "/events",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": limit,
                        "offset": offset,
                    },
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                events.extend(batch)
                if len(batch) < limit:
                    break
                offset += limit
            except Exception as e:
                logger.error(f"Failed to fetch events at offset {offset}: {e}")
                break

        logger.info(f"Fetched {len(events)} active events from Gamma API")
        return events

    def parse_markets_into_events(self, raw_markets: list[dict]) -> list[EventMarket]:
        """Group raw market data into EventMarket objects by event."""
        events_map: dict[str, dict] = {}

        for m in raw_markets:
            # Skip if no CLOB token IDs
            clob_token_ids = m.get("clobTokenIds")
            if not clob_token_ids:
                continue
            if isinstance(clob_token_ids, str):
                import json
                try:
                    clob_token_ids = json.loads(clob_token_ids)
                except (json.JSONDecodeError, TypeError):
                    continue

            outcomes_raw = m.get("outcomes")
            if not outcomes_raw:
                continue
            if isinstance(outcomes_raw, str):
                import json
                try:
                    outcomes_raw = json.loads(outcomes_raw)
                except (json.JSONDecodeError, TypeError):
                    continue

            # Use groupItemTitle or question for event grouping
            event_id = m.get("groupItemTitle") or m.get("question", "")
            condition_id = m.get("conditionId", "")
            market_id = str(m.get("id", ""))
            neg_risk = m.get("negRisk", False)

            if not neg_risk:
                # For binary markets, create a standalone event
                event_key = f"binary_{condition_id}"
                if event_key not in events_map:
                    events_map[event_key] = {
                        "event_id": event_key,
                        "event_slug": m.get("slug", ""),
                        "title": m.get("question", "Unknown"),
                        "outcomes": [],
                        "neg_risk": False,
                        "tick_size": float(m.get("minimumTickSize", "0.01")),
                        "min_order_size": float(m.get("minimumOrderSize", "1.0")),
                    }
                # Add YES/NO as outcomes
                for i, (outcome_label, token_id) in enumerate(
                    zip(outcomes_raw, clob_token_ids)
                ):
                    events_map[event_key]["outcomes"].append(
                        MarketOutcome(
                            token_id=token_id,
                            outcome=outcome_label,
                            condition_id=condition_id,
                            market_id=market_id,
                        )
                    )
            else:
                # NegRisk: group by negRiskMarketID
                neg_risk_id = m.get("negRiskMarketID", "")
                event_key = f"negrisk_{neg_risk_id}" if neg_risk_id else f"negrisk_{condition_id}"
                if event_key not in events_map:
                    events_map[event_key] = {
                        "event_id": event_key,
                        "event_slug": m.get("slug", ""),
                        "title": m.get("groupItemTitle", m.get("question", "Unknown")),
                        "outcomes": [],
                        "neg_risk": True,
                        "tick_size": float(m.get("minimumTickSize", "0.01")),
                        "min_order_size": float(m.get("minimumOrderSize", "1.0")),
                    }
                # For NegRisk, each market is one outcome; the YES token is what we buy
                if len(clob_token_ids) >= 1:
                    events_map[event_key]["outcomes"].append(
                        MarketOutcome(
                            token_id=clob_token_ids[0],  # YES token
                            outcome=m.get("question", outcomes_raw[0] if outcomes_raw else "Unknown"),
                            condition_id=condition_id,
                            market_id=market_id,
                        )
                    )

        # Convert to EventMarket objects
        result = []
        for data in events_map.values():
            outcomes = data.pop("outcomes")
            event = EventMarket(outcomes=outcomes, **data)
            result.append(event)

        return result

    # -------------------------------------------------------------------------
    # CLOB API — Order Books
    # -------------------------------------------------------------------------

    async def get_order_book(self, token_id: str) -> OrderBookSnapshot:
        """Fetch order book for a single token."""
        await self._public_limiter.acquire()
        clob = self._get_clob()
        try:
            raw = await asyncio.to_thread(clob.get_order_book, token_id)
            return self._parse_order_book(token_id, raw)
        except Exception as e:
            logger.error(f"Failed to fetch order book for {token_id}: {e}")
            return OrderBookSnapshot(token_id=token_id, asks=[], bids=[])

    async def get_order_books_batch(
        self, token_ids: list[str]
    ) -> dict[str, OrderBookSnapshot]:
        """Fetch order books for multiple tokens."""
        results = {}

        # Batch in groups to respect rate limits
        batch_size = 20
        for i in range(0, len(token_ids), batch_size):
            batch = token_ids[i : i + batch_size]
            await self._public_limiter.acquire()
            clob = self._get_clob()
            try:
                params = [BookParams(token_id=tid) for tid in batch]
                raw_books = await asyncio.to_thread(clob.get_order_books, params)
                for raw_book in raw_books:
                    # The response structure varies; handle both formats
                    if isinstance(raw_book, dict):
                        asset_id = raw_book.get("asset_id", "")
                        if asset_id:
                            results[asset_id] = self._parse_order_book(asset_id, raw_book)
                    else:
                        # Try to match by position
                        for j, tid in enumerate(batch):
                            if j < len(raw_books):
                                results[tid] = self._parse_order_book(tid, raw_books[j])
                        break
            except Exception as e:
                logger.error(f"Failed batch order book fetch: {e}")
                # Fallback: fetch individually
                for tid in batch:
                    results[tid] = await self.get_order_book(tid)

        return results

    def _parse_order_book(self, token_id: str, raw: dict | object) -> OrderBookSnapshot:
        """Parse raw order book response into OrderBookSnapshot."""
        asks = []
        bids = []

        if isinstance(raw, dict):
            raw_asks = raw.get("asks", [])
            raw_bids = raw.get("bids", [])
        else:
            raw_asks = getattr(raw, "asks", [])
            raw_bids = getattr(raw, "bids", [])

        for ask in raw_asks:
            if isinstance(ask, dict):
                price = float(ask.get("price", 0))
                size = float(ask.get("size", 0))
            else:
                price = float(getattr(ask, "price", 0))
                size = float(getattr(ask, "size", 0))
            if price > 0 and size > 0:
                asks.append(PriceLevel(price=price, size=size))

        for bid in raw_bids:
            if isinstance(bid, dict):
                price = float(bid.get("price", 0))
                size = float(bid.get("size", 0))
            else:
                price = float(getattr(bid, "price", 0))
                size = float(getattr(bid, "size", 0))
            if price > 0 and size > 0:
                bids.append(PriceLevel(price=price, size=size))

        # Sort: asks ascending, bids descending
        asks.sort(key=lambda x: x.price)
        bids.sort(key=lambda x: x.price, reverse=True)

        return OrderBookSnapshot(token_id=token_id, asks=asks, bids=bids)

    # -------------------------------------------------------------------------
    # CLOB API — Order Execution
    # -------------------------------------------------------------------------

    async def place_fok_order(
        self,
        token_id: str,
        price: float,
        size: float,
        tick_size: float = 0.01,
        neg_risk: bool = True,
    ) -> dict | None:
        """Place a Fill-or-Kill BUY order."""
        if not self._authenticated:
            logger.error("Cannot place order: not authenticated")
            return None

        await self._order_limiter.acquire()
        clob = self._get_clob()

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
                fee_rate_bps=0,
                nonce=0,
                expiration=0,
            )
            signed_order = await asyncio.to_thread(
                clob.create_order, order_args
            )
            result = await asyncio.to_thread(
                clob.post_order, signed_order, OrderType.FOK
            )
            logger.info(f"FOK order placed: token={token_id}, price={price}, size={size}")
            return result
        except Exception as e:
            logger.error(f"Failed to place FOK order: {e}")
            return None

    async def place_market_sell(
        self,
        token_id: str,
        size: float,
    ) -> dict | None:
        """Place a market sell order (for exiting partial positions)."""
        if not self._authenticated:
            logger.error("Cannot sell: not authenticated")
            return None

        await self._order_limiter.acquire()
        clob = self._get_clob()

        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=size,
                side="SELL",
            )
            signed_order = await asyncio.to_thread(
                clob.create_market_order, order_args
            )
            result = await asyncio.to_thread(
                clob.post_order, signed_order, OrderType.FOK
            )
            logger.info(f"Market sell placed: token={token_id}, size={size}")
            return result
        except Exception as e:
            logger.error(f"Failed to place market sell: {e}")
            return None

    def get_wallet_address(self) -> str | None:
        """Derive wallet address from private key."""
        if not self.config.private_key:
            return None
        try:
            from eth_account import Account
            acct = Account.from_key(self.config.private_key)
            return acct.address
        except Exception:
            return None

    async def get_wallet_balance(self) -> float | None:
        """Fetch USDC balance on Polygon (via public RPC)."""
        address = self.get_wallet_address()
        if not address:
            return None

        # USDC.e on Polygon
        usdc_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        # balanceOf(address) selector
        data = f"0x70a08231000000000000000000000000{address[2:].lower()}"

        try:
            rpc_url = "https://polygon-rpc.com"
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [{"to": usdc_address, "data": data}, "latest"],
                "id": 1,
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(rpc_url, json=payload)
                result = resp.json().get("result", "0x0")
                balance_raw = int(result, 16)
                return balance_raw / 1e6  # USDC has 6 decimals
        except Exception as e:
            logger.debug(f"Failed to fetch balance: {e}")
            return None

    async def get_fee_rate(self) -> float:
        """Query CLOB API for actual fee rate, with caching.

        Returns the fee rate as a decimal (e.g., 0.02 for 2%).
        Falls back to config value if the API call fails.
        """
        now = time.monotonic()
        if (
            self._cached_fee_rate is not None
            and now - self._fee_rate_fetched_at < self._fee_rate_ttl
        ):
            return self._cached_fee_rate

        try:
            await self._public_limiter.acquire()
            clob = self._get_clob()
            # Try to get tick sizes / fee info from the CLOB API
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.config.clob_url}/tick-size")
                if resp.status_code == 200:
                    data = resp.json()
                    # Look for maker/taker fee fields
                    taker_fee = data.get("taker_fee") or data.get("fee_rate")
                    if taker_fee is not None:
                        self._cached_fee_rate = float(taker_fee)
                        self._fee_rate_fetched_at = now
                        logger.info(f"Fee rate from API: {self._cached_fee_rate}")
                        return self._cached_fee_rate
        except Exception as e:
            logger.debug(f"Failed to fetch fee rate from API: {e}")

        # Fallback to config value
        return self.config.arbitrage.fee_estimate_pct

    async def close(self):
        """Clean up resources."""
        await self._http.aclose()

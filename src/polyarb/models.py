"""Data models for the Polymarket arbitrage bot."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class PriceLevel:
    """A single price level in an order book."""
    price: float
    size: float  # shares available at this price


@dataclass
class MarketOutcome:
    """A single outcome within an event (e.g., 'Biden wins')."""
    token_id: str
    outcome: str  # human-readable label
    condition_id: str
    market_id: str


@dataclass
class EventMarket:
    """A Polymarket event with multiple outcomes."""
    event_id: str
    event_slug: str
    title: str
    outcomes: list[MarketOutcome]
    neg_risk: bool
    tick_size: float
    min_order_size: float
    active: bool = True

    @property
    def num_outcomes(self) -> int:
        return len(self.outcomes)


@dataclass
class OrderBookSnapshot:
    """Order book snapshot for a single outcome token."""
    token_id: str
    asks: list[PriceLevel]
    bids: list[PriceLevel]
    timestamp: float = field(default_factory=time.time)

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_ask_size(self) -> float | None:
        return self.asks[0].size if self.asks else None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def total_ask_liquidity(self) -> float:
        return sum(level.size for level in self.asks)


@dataclass
class ArbOpportunity:
    """A verified arbitrage opportunity."""
    event_id: str
    event_title: str
    outcomes: list[str]  # outcome labels
    token_ids: list[str]  # token IDs per outcome
    fill_prices: list[float]  # actual fill price per outcome (after walking book)
    fill_sizes: list[float]  # available shares at fill price per outcome
    total_cost: float  # sum of fill prices (per share)
    fees_estimate: float  # estimated fees per share
    net_cost: float  # total_cost + fees_estimate
    guaranteed_profit: float  # 1.0 - net_cost (MUST be > 0)
    optimal_shares: float  # max executable shares
    estimated_profit_usd: float  # guaranteed_profit * optimal_shares
    detected_at: float = field(default_factory=time.time)
    neg_risk: bool = True

    @property
    def profit_bps(self) -> float:
        """Profit in basis points."""
        return self.guaranteed_profit * 10000

    @property
    def is_valid(self) -> bool:
        """Double-check that profit is actually guaranteed."""
        return self.guaranteed_profit > 0 and self.optimal_shares > 0


@dataclass
class TradeResult:
    """Result of an attempted arbitrage execution."""
    opportunity: ArbOpportunity
    status: str  # "all_filled", "partial", "failed", "dry_run", "risk_rejected"
    legs_filled: int  # how many legs successfully filled
    legs_total: int  # total legs attempted
    order_ids: list[str | None]  # order ID per leg (None if not placed)
    actual_prices: list[float | None]  # actual fill price per leg
    total_cost_actual: float  # what we actually paid
    shares_filled: float
    expected_profit_usd: float
    timestamp: float = field(default_factory=time.time)
    error: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status == "all_filled"

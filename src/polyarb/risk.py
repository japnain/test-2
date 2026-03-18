"""Risk manager — safety controls and position limits.

Independently verifies profit before approving any trade.
Tracks open positions with entry prices for unrealized P&L.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from polyarb.config import Config
from polyarb.models import ArbOpportunity, TradeResult

logger = logging.getLogger(__name__)


@dataclass
class OpenPosition:
    """Tracks an open arbitrage position."""
    event_id: str
    event_title: str
    token_ids: list[str]
    entry_prices: list[float]
    shares: float
    entry_cost: float
    expected_profit: float
    opened_at: float = field(default_factory=time.time)


class RiskManager:
    """Enforces position limits, daily loss caps, and cooldowns."""

    def __init__(self, config: Config):
        self.config = config
        self.open_positions = 0
        self.daily_loss_usd = 0.0
        self.daily_profit_usd = 0.0
        self.total_invested = 0.0
        self._last_fail_time = 0.0
        self._day_start = time.time()

        # Position tracking for unrealized P&L
        self.positions: dict[str, OpenPosition] = {}

    def pre_check(self, opp: ArbOpportunity) -> tuple[bool, str]:
        """Check if a trade is allowed. Returns (allowed, reason)."""
        self._maybe_reset_daily()

        # Kill switch
        if self.config.risk.kill_switch:
            return False, "Kill switch is ON"

        # Guaranteed profit check (independent verification)
        if opp.guaranteed_profit <= 0:
            return False, f"No guaranteed profit: {opp.guaranteed_profit:.6f}"

        if not opp.is_valid:
            return False, "Opportunity failed validation"

        # Position limits
        if self.open_positions >= self.config.risk.max_open_positions:
            return False, f"Max open positions reached: {self.open_positions}"

        # Position size
        position_value = opp.net_cost * opp.optimal_shares
        if position_value > self.config.risk.max_position_usd:
            return False, f"Position ${position_value:.2f} exceeds max ${self.config.risk.max_position_usd}"

        # Daily loss limit
        if self.daily_loss_usd >= self.config.risk.max_daily_loss_usd:
            return False, f"Daily loss limit hit: ${self.daily_loss_usd:.2f}"

        # Cooldown after failure
        if self._in_cooldown():
            remaining = self.config.risk.cooldown_after_fail_sec - (
                time.time() - self._last_fail_time
            )
            return False, f"In cooldown: {remaining:.0f}s remaining"

        # Per-leg cap
        max_leg_cost = max(opp.fill_prices) * opp.optimal_shares
        if max_leg_cost > self.config.execution.max_order_usd:
            return False, f"Leg cost ${max_leg_cost:.2f} exceeds max ${self.config.execution.max_order_usd}"

        return True, "Approved"

    def record_trade(self, result: TradeResult):
        """Update state after a trade execution."""
        if result.status == "all_filled":
            self.open_positions += 1
            self.total_invested += result.total_cost_actual
            self.daily_profit_usd += result.expected_profit_usd

            # Track position
            self.positions[result.opportunity.event_id] = OpenPosition(
                event_id=result.opportunity.event_id,
                event_title=result.opportunity.event_title,
                token_ids=result.opportunity.token_ids,
                entry_prices=result.opportunity.fill_prices,
                shares=result.shares_filled,
                entry_cost=result.total_cost_actual,
                expected_profit=result.expected_profit_usd,
            )

            logger.info(
                f"Trade recorded: +${result.expected_profit_usd:.2f} expected | "
                f"Open positions: {self.open_positions}"
            )

        elif result.status == "partial":
            self.open_positions += 1
            self._last_fail_time = time.time()
            # Assume worst case on partial: we lose the filled amount
            self.daily_loss_usd += result.total_cost_actual
            logger.warning(
                f"Partial trade recorded as loss: -${result.total_cost_actual:.2f} | "
                f"Daily loss: ${self.daily_loss_usd:.2f}"
            )

        elif result.status == "failed":
            self._last_fail_time = time.time()
            logger.info("Failed trade recorded, cooldown started")

    def record_resolution(self, event_id: str, profit_usd: float):
        """Called when a position resolves (market settles)."""
        self.open_positions = max(0, self.open_positions - 1)
        self.positions.pop(event_id, None)
        if profit_usd >= 0:
            self.daily_profit_usd += profit_usd
        else:
            self.daily_loss_usd += abs(profit_usd)

    def get_unrealized_pnl(self) -> float:
        """Calculate total unrealized P&L from open positions.

        For arb positions, the unrealized P&L is the expected profit
        (since resolution is guaranteed to pay $1.00 per share for one outcome).
        """
        return sum(pos.expected_profit for pos in self.positions.values())

    def get_position_age_seconds(self, event_id: str) -> float | None:
        """Get how long a position has been open."""
        pos = self.positions.get(event_id)
        if pos is None:
            return None
        return time.time() - pos.opened_at

    def _in_cooldown(self) -> bool:
        if self._last_fail_time == 0:
            return False
        return (
            time.time() - self._last_fail_time
            < self.config.risk.cooldown_after_fail_sec
        )

    def _maybe_reset_daily(self):
        """Reset daily counters at midnight."""
        now = time.time()
        if now - self._day_start > 86400:
            self.daily_loss_usd = 0.0
            self.daily_profit_usd = 0.0
            self._day_start = now
            logger.info("Daily counters reset")

    @property
    def summary(self) -> dict:
        return {
            "open_positions": self.open_positions,
            "daily_profit_usd": round(self.daily_profit_usd, 2),
            "daily_loss_usd": round(self.daily_loss_usd, 2),
            "total_invested": round(self.total_invested, 2),
            "unrealized_pnl": round(self.get_unrealized_pnl(), 2),
            "in_cooldown": self._in_cooldown(),
            "kill_switch": self.config.risk.kill_switch,
        }

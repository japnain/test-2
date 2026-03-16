"""Risk manager — safety controls and position limits.

Independently verifies profit before approving any trade.
"""

from __future__ import annotations

import logging
import time

from polyarb.config import Config
from polyarb.models import ArbOpportunity, TradeResult

logger = logging.getLogger(__name__)


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

    def record_resolution(self, profit_usd: float):
        """Called when a position resolves (market settles)."""
        self.open_positions = max(0, self.open_positions - 1)
        if profit_usd >= 0:
            self.daily_profit_usd += profit_usd
        else:
            self.daily_loss_usd += abs(profit_usd)

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
            "in_cooldown": self._in_cooldown(),
            "kill_switch": self.config.risk.kill_switch,
        }

"""P&L tracker — logs trades and tracks cumulative performance."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from polyarb.config import Config
from polyarb.models import ArbOpportunity, TradeResult

logger = logging.getLogger(__name__)


class PnLTracker:
    """Tracks all trades, opportunities, and cumulative P&L."""

    def __init__(self, config: Config):
        self.config = config
        self.trade_log_path = Path(config.logging.trade_log)
        self.trade_log_path.parent.mkdir(parents=True, exist_ok=True)

        # Cumulative stats
        self.total_trades = 0
        self.successful_trades = 0
        self.failed_trades = 0
        self.partial_trades = 0
        self.dry_run_trades = 0

        self.total_invested = 0.0
        self.total_expected_profit = 0.0
        self.total_actual_profit = 0.0

        # Opportunity tracking
        self.total_opportunities_detected = 0
        self.opportunities_log: list[dict] = []  # Last N for dashboard

    def record_opportunity(self, opp: ArbOpportunity):
        """Log a detected opportunity (whether executed or not)."""
        self.total_opportunities_detected += 1
        entry = {
            "type": "opportunity",
            "timestamp": time.time(),
            "event_id": opp.event_id,
            "event_title": opp.event_title,
            "outcomes": opp.outcomes,
            "total_cost": opp.total_cost,
            "guaranteed_profit": opp.guaranteed_profit,
            "profit_bps": opp.profit_bps,
            "optimal_shares": opp.optimal_shares,
            "estimated_profit_usd": opp.estimated_profit_usd,
        }
        self._append_log(entry)
        self.opportunities_log.append(entry)
        # Keep last 50 for dashboard
        if len(self.opportunities_log) > 50:
            self.opportunities_log = self.opportunities_log[-50:]

    def record_trade(self, result: TradeResult):
        """Log a trade execution result.

        In dry-run mode, trades come through as 'all_filled' (same as live)
        since the executor now runs the full pipeline with simulated orders.
        """
        self.total_trades += 1

        if result.status == "all_filled":
            self.successful_trades += 1
            self.total_invested += result.total_cost_actual
            self.total_expected_profit += result.expected_profit_usd
        elif result.status == "partial":
            self.partial_trades += 1
        elif result.status == "dry_run":
            # Legacy compat — should not happen with new executor
            self.dry_run_trades += 1
            self.total_expected_profit += result.expected_profit_usd
        else:
            self.failed_trades += 1

        entry = {
            "type": "trade",
            "timestamp": result.timestamp,
            "event_id": result.opportunity.event_id,
            "event_title": result.opportunity.event_title,
            "status": result.status,
            "legs_filled": result.legs_filled,
            "legs_total": result.legs_total,
            "shares_filled": result.shares_filled,
            "total_cost": result.total_cost_actual,
            "expected_profit_usd": result.expected_profit_usd,
            "error": result.error,
        }
        self._append_log(entry)

    def _append_log(self, entry: dict):
        """Append entry to JSONL trade log."""
        try:
            with open(self.trade_log_path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error(f"Failed to write trade log: {e}")

    @property
    def summary(self) -> dict:
        win_rate = (
            self.successful_trades / max(1, self.total_trades - self.dry_run_trades)
            if (self.total_trades - self.dry_run_trades) > 0
            else 0
        )
        return {
            "total_trades": self.total_trades,
            "successful": self.successful_trades,
            "failed": self.failed_trades,
            "partial": self.partial_trades,
            "dry_run": self.dry_run_trades,
            "win_rate": f"{win_rate:.0%}",
            "total_invested": f"${self.total_invested:.2f}",
            "expected_profit": f"${self.total_expected_profit:.2f}",
            "opportunities_detected": self.total_opportunities_detected,
        }

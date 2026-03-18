"""Tests for the risk manager."""

import time

import pytest

from polyarb.config import Config
from polyarb.models import ArbOpportunity, TradeResult
from polyarb.risk import RiskManager


def make_config() -> Config:
    config = Config()
    config.risk.max_position_usd = 500.0
    config.risk.max_open_positions = 5
    config.risk.max_daily_loss_usd = 100.0
    config.risk.cooldown_after_fail_sec = 10.0
    config.risk.kill_switch = False
    config.execution.max_order_usd = 100.0
    return config


def make_opp(
    profit: float = 0.05,
    shares: float = 10.0,
    fill_prices: list[float] | None = None,
    event_id: str = "test",
) -> ArbOpportunity:
    if fill_prices is None:
        fill_prices = [0.30, 0.30, 0.30]
    total_cost = sum(fill_prices)
    return ArbOpportunity(
        event_id=event_id,
        event_title="Test Event",
        outcomes=["A", "B", "C"],
        token_ids=["t1", "t2", "t3"],
        fill_prices=fill_prices,
        fill_sizes=[100.0] * len(fill_prices),
        total_cost=total_cost,
        fees_estimate=0.02,
        net_cost=total_cost + 0.02,
        guaranteed_profit=profit,
        optimal_shares=shares,
        estimated_profit_usd=profit * shares,
    )


class TestRiskManager:
    def test_allows_valid_trade(self):
        risk = RiskManager(make_config())
        opp = make_opp(profit=0.05, shares=10)
        allowed, reason = risk.pre_check(opp)
        assert allowed
        assert reason == "Approved"

    def test_rejects_zero_profit(self):
        risk = RiskManager(make_config())
        opp = make_opp(profit=0.0)
        allowed, reason = risk.pre_check(opp)
        assert not allowed
        assert "No guaranteed profit" in reason

    def test_rejects_negative_profit(self):
        risk = RiskManager(make_config())
        opp = make_opp(profit=-0.01)
        allowed, reason = risk.pre_check(opp)
        assert not allowed

    def test_kill_switch(self):
        config = make_config()
        config.risk.kill_switch = True
        risk = RiskManager(config)
        opp = make_opp()
        allowed, reason = risk.pre_check(opp)
        assert not allowed
        assert "Kill switch" in reason

    def test_max_positions(self):
        config = make_config()
        config.risk.max_open_positions = 2
        risk = RiskManager(config)

        opp = make_opp()
        result = TradeResult(
            opportunity=opp,
            status="all_filled",
            legs_filled=3,
            legs_total=3,
            order_ids=["o1", "o2", "o3"],
            actual_prices=[0.30, 0.30, 0.30],
            total_cost_actual=9.0,
            shares_filled=10,
            expected_profit_usd=0.50,
        )

        # Fill up positions
        risk.record_trade(result)
        risk.record_trade(result)

        # Now at max
        allowed, reason = risk.pre_check(make_opp())
        assert not allowed
        assert "Max open positions" in reason

    def test_position_size_limit(self):
        config = make_config()
        config.risk.max_position_usd = 10.0
        risk = RiskManager(config)

        # This trade would cost 0.92 * 50 = 46 USD — exceeds max
        opp = make_opp(profit=0.06, shares=50, fill_prices=[0.30, 0.30, 0.30])
        allowed, reason = risk.pre_check(opp)
        assert not allowed
        assert "exceeds max" in reason

    def test_daily_loss_limit(self):
        config = make_config()
        config.risk.max_daily_loss_usd = 10.0
        risk = RiskManager(config)

        # Record a partial (counts as loss)
        opp = make_opp()
        partial_result = TradeResult(
            opportunity=opp,
            status="partial",
            legs_filled=1,
            legs_total=3,
            order_ids=["o1", None, None],
            actual_prices=[0.30, None, None],
            total_cost_actual=15.0,
            shares_filled=0,
            expected_profit_usd=0,
        )
        risk.record_trade(partial_result)

        # Daily loss should block further trades
        allowed, reason = risk.pre_check(make_opp())
        assert not allowed
        assert "Daily loss limit" in reason

    def test_cooldown_after_failure(self):
        config = make_config()
        config.risk.cooldown_after_fail_sec = 100  # Long cooldown for test
        risk = RiskManager(config)

        failed_result = TradeResult(
            opportunity=make_opp(),
            status="failed",
            legs_filled=0,
            legs_total=3,
            order_ids=[],
            actual_prices=[],
            total_cost_actual=0,
            shares_filled=0,
            expected_profit_usd=0,
        )
        risk.record_trade(failed_result)

        # Should be in cooldown
        allowed, reason = risk.pre_check(make_opp())
        assert not allowed
        assert "cooldown" in reason.lower()

    def test_summary(self):
        risk = RiskManager(make_config())
        summary = risk.summary
        assert "open_positions" in summary
        assert "daily_profit_usd" in summary
        assert "kill_switch" in summary
        assert "unrealized_pnl" in summary

    def test_position_tracking_on_fill(self):
        """Verify positions are tracked when trades fill."""
        risk = RiskManager(make_config())
        opp = make_opp(profit=0.05, shares=10, event_id="event_1")
        result = TradeResult(
            opportunity=opp,
            status="all_filled",
            legs_filled=3,
            legs_total=3,
            order_ids=["o1", "o2", "o3"],
            actual_prices=[0.30, 0.30, 0.30],
            total_cost_actual=9.0,
            shares_filled=10,
            expected_profit_usd=0.50,
        )
        risk.record_trade(result)

        assert "event_1" in risk.positions
        pos = risk.positions["event_1"]
        assert pos.shares == 10
        assert pos.expected_profit == 0.50

    def test_unrealized_pnl(self):
        """Verify unrealized P&L is tracked from open positions."""
        risk = RiskManager(make_config())

        # Add two positions
        for i, profit in enumerate([0.50, 0.30]):
            opp = make_opp(profit=0.05, shares=10, event_id=f"event_{i}")
            result = TradeResult(
                opportunity=opp,
                status="all_filled",
                legs_filled=3,
                legs_total=3,
                order_ids=["o1", "o2", "o3"],
                actual_prices=[0.30, 0.30, 0.30],
                total_cost_actual=9.0,
                shares_filled=10,
                expected_profit_usd=profit,
            )
            risk.record_trade(result)

        assert risk.get_unrealized_pnl() == pytest.approx(0.80)

    def test_resolution_removes_position(self):
        """Verify resolution removes tracked position."""
        risk = RiskManager(make_config())
        opp = make_opp(profit=0.05, shares=10, event_id="event_1")
        result = TradeResult(
            opportunity=opp,
            status="all_filled",
            legs_filled=3,
            legs_total=3,
            order_ids=["o1", "o2", "o3"],
            actual_prices=[0.30, 0.30, 0.30],
            total_cost_actual=9.0,
            shares_filled=10,
            expected_profit_usd=0.50,
        )
        risk.record_trade(result)
        assert "event_1" in risk.positions

        risk.record_resolution("event_1", 0.50)
        assert "event_1" not in risk.positions
        assert risk.open_positions == 0

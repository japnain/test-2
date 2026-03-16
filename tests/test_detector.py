"""Tests for the arbitrage detector — verify profit math is bulletproof."""

import pytest

from polyarb.config import Config
from polyarb.detector import ArbDetector, walk_book, find_optimal_shares
from polyarb.models import (
    EventMarket,
    MarketOutcome,
    OrderBookSnapshot,
    PriceLevel,
)


def make_config(**overrides) -> Config:
    config = Config()
    config.arbitrage.min_profit_bps = 30
    config.arbitrage.min_profit_usd = 0.01
    config.arbitrage.fee_estimate_pct = 0.02
    config.execution.max_order_usd = 100.0
    for k, v in overrides.items():
        setattr(config.arbitrage, k, v)
    return config


def make_book(asks: list[tuple[float, float]], bids=None) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id="test",
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
        bids=[PriceLevel(price=p, size=s) for p, s in (bids or [])],
    )


class TestWalkBook:
    def test_single_level_exact(self):
        asks = [PriceLevel(0.30, 100)]
        result = walk_book(asks, 100)
        assert result is not None
        avg_price, filled = result
        assert avg_price == pytest.approx(0.30)
        assert filled == pytest.approx(100)

    def test_single_level_partial(self):
        asks = [PriceLevel(0.30, 50)]
        result = walk_book(asks, 100)
        assert result is not None
        avg_price, filled = result
        assert avg_price == pytest.approx(0.30)
        assert filled == pytest.approx(50)

    def test_multiple_levels(self):
        asks = [PriceLevel(0.30, 50), PriceLevel(0.35, 50)]
        result = walk_book(asks, 100)
        assert result is not None
        avg_price, filled = result
        # 50*0.30 + 50*0.35 = 15 + 17.5 = 32.5 / 100 = 0.325
        assert avg_price == pytest.approx(0.325)
        assert filled == pytest.approx(100)

    def test_empty_book(self):
        assert walk_book([], 10) is None

    def test_zero_shares(self):
        asks = [PriceLevel(0.30, 100)]
        assert walk_book(asks, 0) is None

    def test_deep_book_walk(self):
        asks = [
            PriceLevel(0.20, 10),
            PriceLevel(0.25, 20),
            PriceLevel(0.30, 30),
            PriceLevel(0.40, 40),
        ]
        result = walk_book(asks, 50)
        assert result is not None
        avg_price, filled = result
        # 10*0.20 + 20*0.25 + 20*0.30 = 2 + 5 + 6 = 13 / 50 = 0.26
        assert avg_price == pytest.approx(0.26)
        assert filled == pytest.approx(50)


class TestFindOptimalShares:
    def test_profitable_arb(self):
        # 3 outcomes, sum of asks = 0.90, fees = 0.02, profit = 0.08/share
        asks = [
            [PriceLevel(0.30, 100)],
            [PriceLevel(0.30, 100)],
            [PriceLevel(0.30, 100)],
        ]
        result = find_optimal_shares(asks, fee_estimate_pct=0.02, min_profit_bps=30)
        assert result is not None
        shares, prices = result
        assert shares > 0
        assert len(prices) == 3
        assert sum(prices) == pytest.approx(0.90)

    def test_no_arb_sum_equals_one(self):
        asks = [
            [PriceLevel(0.50, 100)],
            [PriceLevel(0.50, 100)],
        ]
        result = find_optimal_shares(asks, fee_estimate_pct=0.02, min_profit_bps=30)
        assert result is None

    def test_no_arb_sum_above_one(self):
        asks = [
            [PriceLevel(0.60, 100)],
            [PriceLevel(0.60, 100)],
        ]
        result = find_optimal_shares(asks, fee_estimate_pct=0.02, min_profit_bps=30)
        assert result is None

    def test_no_liquidity(self):
        asks = [
            [PriceLevel(0.30, 0)],
            [PriceLevel(0.30, 100)],
        ]
        result = find_optimal_shares(asks, fee_estimate_pct=0.02, min_profit_bps=30)
        assert result is None

    def test_limited_by_thin_leg(self):
        asks = [
            [PriceLevel(0.30, 5)],   # Only 5 shares available
            [PriceLevel(0.30, 100)],
            [PriceLevel(0.30, 100)],
        ]
        result = find_optimal_shares(asks, fee_estimate_pct=0.02, min_profit_bps=30)
        assert result is not None
        shares, _ = result
        assert shares <= 5  # Limited by thinnest leg


class TestArbDetector:
    def test_detects_multi_outcome_arb(self):
        config = make_config()
        detector = ArbDetector(config)

        event = EventMarket(
            event_id="test_event",
            event_slug="test",
            title="Who wins?",
            outcomes=[
                MarketOutcome("tok_a", "Alice", "cond_a", "mkt_a"),
                MarketOutcome("tok_b", "Bob", "cond_b", "mkt_b"),
                MarketOutcome("tok_c", "Carol", "cond_c", "mkt_c"),
            ],
            neg_risk=True,
            tick_size=0.01,
            min_order_size=1.0,
        )

        books = {
            "tok_a": make_book([(0.25, 100)]),
            "tok_b": make_book([(0.25, 100)]),
            "tok_c": make_book([(0.25, 100)]),
        }

        opps = detector.evaluate([(event, books)])
        assert len(opps) == 1
        opp = opps[0]
        assert opp.guaranteed_profit > 0
        assert opp.total_cost == pytest.approx(0.75)
        assert opp.is_valid

    def test_rejects_no_arb(self):
        config = make_config()
        detector = ArbDetector(config)

        event = EventMarket(
            event_id="test_event",
            event_slug="test",
            title="Who wins?",
            outcomes=[
                MarketOutcome("tok_a", "Alice", "cond_a", "mkt_a"),
                MarketOutcome("tok_b", "Bob", "cond_b", "mkt_b"),
            ],
            neg_risk=True,
            tick_size=0.01,
            min_order_size=1.0,
        )

        # Sum = 1.10 — no arb
        books = {
            "tok_a": make_book([(0.55, 100)]),
            "tok_b": make_book([(0.55, 100)]),
        }

        opps = detector.evaluate([(event, books)])
        assert len(opps) == 0

    def test_rejects_when_fees_eat_profit(self):
        config = make_config(fee_estimate_pct=0.05)  # 5% fee
        detector = ArbDetector(config)

        event = EventMarket(
            event_id="test_event",
            event_slug="test",
            title="Tight race",
            outcomes=[
                MarketOutcome("tok_a", "Alice", "cond_a", "mkt_a"),
                MarketOutcome("tok_b", "Bob", "cond_b", "mkt_b"),
            ],
            neg_risk=True,
            tick_size=0.01,
            min_order_size=1.0,
        )

        # Sum = 0.96, but 5% fee = 0.05, net cost = 1.01 — no arb
        books = {
            "tok_a": make_book([(0.48, 100)]),
            "tok_b": make_book([(0.48, 100)]),
        }

        opps = detector.evaluate([(event, books)])
        assert len(opps) == 0

    def test_binary_arb_detection(self):
        config = make_config(fee_estimate_pct=0.01)
        detector = ArbDetector(config)

        event = EventMarket(
            event_id="binary_test",
            event_slug="test",
            title="Will X happen?",
            outcomes=[
                MarketOutcome("tok_yes", "Yes", "cond_1", "mkt_1"),
                MarketOutcome("tok_no", "No", "cond_1", "mkt_1"),
            ],
            neg_risk=False,
            tick_size=0.01,
            min_order_size=1.0,
        )

        # YES=0.40, NO=0.45, sum=0.85, fee=0.01, net=0.86, profit=0.14
        books = {
            "tok_yes": make_book([(0.40, 50)]),
            "tok_no": make_book([(0.45, 50)]),
        }

        opps = detector.evaluate([(event, books)])
        assert len(opps) == 1
        assert opps[0].guaranteed_profit > 0

    def test_missing_book_returns_no_arb(self):
        config = make_config()
        detector = ArbDetector(config)

        event = EventMarket(
            event_id="test",
            event_slug="test",
            title="Test",
            outcomes=[
                MarketOutcome("tok_a", "A", "c1", "m1"),
                MarketOutcome("tok_b", "B", "c2", "m2"),
            ],
            neg_risk=True,
            tick_size=0.01,
            min_order_size=1.0,
        )

        # tok_b has no book data
        books = {
            "tok_a": make_book([(0.30, 100)]),
        }

        opps = detector.evaluate([(event, books)])
        assert len(opps) == 0

    def test_guaranteed_profit_is_never_negative(self):
        """The detector must NEVER return an opportunity with negative profit."""
        config = make_config(min_profit_bps=0, min_profit_usd=0)
        detector = ArbDetector(config)

        # Edge case: sum exactly equals 1.0 - fees
        event = EventMarket(
            event_id="edge",
            event_slug="edge",
            title="Edge case",
            outcomes=[
                MarketOutcome("tok_a", "A", "c1", "m1"),
                MarketOutcome("tok_b", "B", "c2", "m2"),
                MarketOutcome("tok_c", "C", "c3", "m3"),
            ],
            neg_risk=True,
            tick_size=0.01,
            min_order_size=1.0,
        )

        # Sum = 0.99, fee = 0.02, net = 1.01 — should NOT be detected
        books = {
            "tok_a": make_book([(0.33, 100)]),
            "tok_b": make_book([(0.33, 100)]),
            "tok_c": make_book([(0.33, 100)]),
        }

        opps = detector.evaluate([(event, books)])
        for opp in opps:
            assert opp.guaranteed_profit > 0, "Profit must always be positive!"

"""Arbitrage detection engine — THE CORE.

RULE: No trade is EVER placed unless profit is mathematically guaranteed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from polyarb.config import Config
from polyarb.models import (
    ArbOpportunity,
    EventMarket,
    OrderBookSnapshot,
    PriceLevel,
)

logger = logging.getLogger(__name__)


def walk_book(asks: list[PriceLevel], target_shares: float) -> tuple[float, float] | None:
    """Walk the order book to find actual fill price for target_shares.

    Returns (average_fill_price, fillable_shares) or None if no liquidity.
    Accounts for partial fills at each price level.
    """
    if not asks or target_shares <= 0:
        return None

    total_cost = 0.0
    shares_filled = 0.0

    for level in asks:
        available = level.size
        needed = target_shares - shares_filled
        fill_at_level = min(available, needed)
        total_cost += fill_at_level * level.price
        shares_filled += fill_at_level
        if shares_filled >= target_shares:
            break

    if shares_filled <= 0:
        return None

    avg_price = total_cost / shares_filled
    return avg_price, shares_filled


def find_optimal_shares(
    asks_per_outcome: list[list[PriceLevel]],
    fee_estimate_pct: float,
    min_profit_bps: float,
) -> tuple[float, list[float]] | None:
    """Find the optimal number of shares that maximizes total USD profit.

    Walks all books simultaneously, increasing share count until marginal
    profit turns negative.

    Returns (optimal_shares, fill_prices_per_outcome) or None.
    """
    if not asks_per_outcome:
        return None

    # Find the max shares we could possibly fill (limited by thinnest leg)
    max_fillable = []
    for asks in asks_per_outcome:
        total_liquidity = sum(level.size for level in asks)
        max_fillable.append(total_liquidity)

    if not max_fillable or min(max_fillable) <= 0:
        return None

    max_shares = min(max_fillable)

    # Binary search for optimal share count
    best_shares = 0.0
    best_profit = 0.0
    best_prices: list[float] = []

    # Test share sizes from small to large
    test_sizes = []
    step = max(0.1, max_shares / 100)
    s = step
    while s <= max_shares:
        test_sizes.append(s)
        s += step

    if not test_sizes:
        test_sizes = [max_shares]

    for test_share in test_sizes:
        prices = []
        total_cost = 0.0
        valid = True

        for asks in asks_per_outcome:
            result = walk_book(asks, test_share)
            if result is None:
                valid = False
                break
            avg_price, filled = result
            if filled < test_share * 0.99:  # Need at least 99% fill
                valid = False
                break
            prices.append(avg_price)
            total_cost += avg_price

        if not valid:
            break

        # Check profitability at this size
        net_cost = total_cost + fee_estimate_pct
        profit_per_share = 1.0 - net_cost

        if profit_per_share <= min_profit_bps / 10000:
            # Not profitable at this depth — stop here
            break

        total_profit = profit_per_share * test_share
        if total_profit > best_profit:
            best_profit = total_profit
            best_shares = test_share
            best_prices = prices

    if best_shares <= 0 or not best_prices:
        return None

    return best_shares, best_prices


class ArbDetector:
    """Detects arbitrage opportunities across Polymarket events."""

    def __init__(self, config: Config):
        self.config = config
        self.opportunities_found = 0

    def evaluate(
        self,
        events_with_books: list[tuple[EventMarket, dict[str, OrderBookSnapshot]]],
    ) -> list[ArbOpportunity]:
        """Evaluate all events for arbitrage opportunities."""
        opportunities = []

        for event, books in events_with_books:
            if event.neg_risk and event.num_outcomes >= 2:
                opp = self._detect_multi_outcome(event, books)
                if opp and opp.is_valid:
                    opportunities.append(opp)

            elif not event.neg_risk and event.num_outcomes == 2:
                opp = self._detect_binary(event, books)
                if opp and opp.is_valid:
                    opportunities.append(opp)

        # Sort by estimated profit (highest first)
        opportunities.sort(key=lambda o: -o.estimated_profit_usd)
        self.opportunities_found += len(opportunities)
        return opportunities

    def _detect_multi_outcome(
        self, event: EventMarket, books: dict[str, OrderBookSnapshot]
    ) -> ArbOpportunity | None:
        """Detect arbitrage in multi-outcome NegRisk markets.

        For an event with N outcomes, if sum of YES ask prices < $1.00 (after fees),
        buying YES on every outcome guarantees profit since exactly one pays $1.00.
        """
        outcome_labels = []
        token_ids = []
        asks_per_outcome = []

        for outcome in event.outcomes:
            book = books.get(outcome.token_id)
            if not book or not book.asks:
                return None  # Need asks for ALL outcomes

            outcome_labels.append(outcome.outcome)
            token_ids.append(outcome.token_id)
            asks_per_outcome.append(book.asks)

        # Quick check: sum of best asks
        best_ask_sum = sum(
            asks[0].price for asks in asks_per_outcome if asks
        )
        fee_est = self.config.arbitrage.fee_estimate_pct

        if best_ask_sum + fee_est >= 1.0:
            return None  # No arb at best ask level

        # Deep analysis: find optimal shares with book walking
        result = find_optimal_shares(
            asks_per_outcome,
            fee_est,
            self.config.arbitrage.min_profit_bps,
        )
        if result is None:
            return None

        optimal_shares, fill_prices = result
        total_cost = sum(fill_prices)
        net_cost = total_cost + fee_est
        guaranteed_profit = 1.0 - net_cost

        if guaranteed_profit <= 0:
            return None

        # Check against USD minimum
        estimated_profit_usd = guaranteed_profit * optimal_shares
        if estimated_profit_usd < self.config.arbitrage.min_profit_usd:
            return None

        # Cap by max order USD
        max_shares_by_budget = self.config.execution.max_order_usd / max(fill_prices)
        if optimal_shares > max_shares_by_budget:
            optimal_shares = max_shares_by_budget
            estimated_profit_usd = guaranteed_profit * optimal_shares

        # Get liquidity per leg
        fill_sizes = []
        for asks in asks_per_outcome:
            result = walk_book(asks, optimal_shares)
            fill_sizes.append(result[1] if result else 0.0)

        opp = ArbOpportunity(
            event_id=event.event_id,
            event_title=event.title,
            outcomes=outcome_labels,
            token_ids=token_ids,
            fill_prices=fill_prices,
            fill_sizes=fill_sizes,
            total_cost=total_cost,
            fees_estimate=fee_est,
            net_cost=net_cost,
            guaranteed_profit=guaranteed_profit,
            optimal_shares=optimal_shares,
            estimated_profit_usd=estimated_profit_usd,
            neg_risk=True,
        )

        logger.info(
            f"ARB FOUND: {event.title} | "
            f"{len(outcome_labels)} outcomes | "
            f"cost={total_cost:.4f} | "
            f"profit={guaranteed_profit:.4f}/share | "
            f"shares={optimal_shares:.1f} | "
            f"est_profit=${estimated_profit_usd:.2f}"
        )

        return opp

    def _detect_binary(
        self, event: EventMarket, books: dict[str, OrderBookSnapshot]
    ) -> ArbOpportunity | None:
        """Detect arbitrage in binary YES/NO markets.

        If YES_ask + NO_ask + fees < $1.00, buying both guarantees profit.
        Note: This is rare due to shared order books but can happen during volatility.
        """
        if len(event.outcomes) != 2:
            return None

        yes_outcome = event.outcomes[0]
        no_outcome = event.outcomes[1]

        yes_book = books.get(yes_outcome.token_id)
        no_book = books.get(no_outcome.token_id)

        if not yes_book or not yes_book.asks or not no_book or not no_book.asks:
            return None

        asks_per_outcome = [yes_book.asks, no_book.asks]
        fee_est = self.config.arbitrage.fee_estimate_pct

        # Quick check
        best_ask_sum = yes_book.asks[0].price + no_book.asks[0].price
        if best_ask_sum + fee_est >= 1.0:
            return None

        # Deep analysis
        result = find_optimal_shares(
            asks_per_outcome,
            fee_est,
            self.config.arbitrage.min_profit_bps,
        )
        if result is None:
            return None

        optimal_shares, fill_prices = result
        total_cost = sum(fill_prices)
        net_cost = total_cost + fee_est
        guaranteed_profit = 1.0 - net_cost

        if guaranteed_profit <= 0:
            return None

        estimated_profit_usd = guaranteed_profit * optimal_shares
        if estimated_profit_usd < self.config.arbitrage.min_profit_usd:
            return None

        fill_sizes = []
        for asks in asks_per_outcome:
            r = walk_book(asks, optimal_shares)
            fill_sizes.append(r[1] if r else 0.0)

        opp = ArbOpportunity(
            event_id=event.event_id,
            event_title=event.title,
            outcomes=[yes_outcome.outcome, no_outcome.outcome],
            token_ids=[yes_outcome.token_id, no_outcome.token_id],
            fill_prices=fill_prices,
            fill_sizes=fill_sizes,
            total_cost=total_cost,
            fees_estimate=fee_est,
            net_cost=net_cost,
            guaranteed_profit=guaranteed_profit,
            optimal_shares=optimal_shares,
            estimated_profit_usd=estimated_profit_usd,
            neg_risk=False,
        )

        logger.info(
            f"BINARY ARB FOUND: {event.title} | "
            f"YES={fill_prices[0]:.3f} NO={fill_prices[1]:.3f} | "
            f"profit={guaranteed_profit:.4f}/share | "
            f"est_profit=${estimated_profit_usd:.2f}"
        )

        return opp

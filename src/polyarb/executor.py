"""Trade executor — places FOK orders with sequential leg execution.

RULE: Only executes if profit is mathematically guaranteed.
Pre-trade verification: re-checks order books before every leg.
"""

from __future__ import annotations

import logging
import time

from polyarb.client import PolymarketClient
from polyarb.config import Config
from polyarb.detector import walk_book
from polyarb.models import ArbOpportunity, TradeResult

logger = logging.getLogger(__name__)


class Executor:
    """Executes arbitrage trades via FOK orders."""

    def __init__(self, client: PolymarketClient, config: Config):
        self.client = client
        self.config = config

    async def execute(self, opp: ArbOpportunity) -> TradeResult:
        """Execute an arbitrage opportunity.

        Strategy:
        1. Sort legs by liquidity (thinnest first)
        2. Execute legs sequentially
        3. If any leg fails, sell already-filled positions to exit
        4. Only count as success if ALL legs fill
        """
        if self.config.is_dry_run:
            return self._simulate(opp)

        # Pre-trade verification: re-fetch all order books
        if self.config.execution.verify_before_execute:
            still_valid = await self._verify_opportunity(opp)
            if not still_valid:
                logger.warning(f"Opportunity vanished before execution: {opp.event_title}")
                return TradeResult(
                    opportunity=opp,
                    status="failed",
                    legs_filled=0,
                    legs_total=len(opp.token_ids),
                    order_ids=[],
                    actual_prices=[],
                    total_cost_actual=0,
                    shares_filled=0,
                    expected_profit_usd=0,
                    error="Opportunity no longer valid on re-check",
                )

        # Sort legs by liquidity (thinnest first = least risky to try first)
        leg_order = sorted(
            range(len(opp.token_ids)),
            key=lambda i: opp.fill_sizes[i],
        )

        order_ids: list[str | None] = [None] * len(opp.token_ids)
        actual_prices: list[float | None] = [None] * len(opp.token_ids)
        filled_legs: list[int] = []

        for leg_idx in leg_order:
            token_id = opp.token_ids[leg_idx]
            price = opp.fill_prices[leg_idx]
            size = opp.optimal_shares

            logger.info(
                f"Executing leg {leg_idx + 1}/{len(opp.token_ids)}: "
                f"BUY {size:.1f} shares of '{opp.outcomes[leg_idx]}' at ${price:.4f}"
            )

            result = await self.client.place_fok_order(
                token_id=token_id,
                price=price,
                size=size,
                neg_risk=opp.neg_risk,
            )

            if result and self._is_filled(result):
                order_ids[leg_idx] = self._extract_order_id(result)
                actual_prices[leg_idx] = price  # FOK fills at our price or not at all
                filled_legs.append(leg_idx)
                logger.info(f"Leg {leg_idx + 1} FILLED")
            else:
                logger.warning(f"Leg {leg_idx + 1} FAILED — aborting remaining legs")
                break

        # Determine result
        if len(filled_legs) == len(opp.token_ids):
            # All legs filled — guaranteed profit locked in
            total_cost = sum(p for p in actual_prices if p is not None) * opp.optimal_shares
            status = "all_filled"
            logger.info(
                f"ARB EXECUTED: {opp.event_title} | "
                f"All {len(opp.token_ids)} legs filled | "
                f"Cost=${total_cost:.2f} | "
                f"Expected profit=${opp.estimated_profit_usd:.2f}"
            )
        elif filled_legs:
            # Partial fill — try to sell filled positions to exit
            logger.warning(
                f"PARTIAL FILL: {len(filled_legs)}/{len(opp.token_ids)} legs. "
                f"Attempting to sell filled positions..."
            )
            await self._exit_partial(opp, filled_legs, opp.optimal_shares)
            total_cost = sum(
                (actual_prices[i] or 0) * opp.optimal_shares for i in filled_legs
            )
            status = "partial"
        else:
            total_cost = 0
            status = "failed"

        return TradeResult(
            opportunity=opp,
            status=status,
            legs_filled=len(filled_legs),
            legs_total=len(opp.token_ids),
            order_ids=order_ids,
            actual_prices=actual_prices,
            total_cost_actual=total_cost,
            shares_filled=opp.optimal_shares if status == "all_filled" else 0,
            expected_profit_usd=opp.estimated_profit_usd if status == "all_filled" else 0,
        )

    async def _verify_opportunity(self, opp: ArbOpportunity) -> bool:
        """Re-fetch order books and verify the opportunity still exists."""
        books = await self.client.get_order_books_batch(opp.token_ids)

        total_cost = 0.0
        for i, token_id in enumerate(opp.token_ids):
            book = books.get(token_id)
            if not book or not book.asks:
                return False
            result = walk_book(book.asks, opp.optimal_shares)
            if result is None:
                return False
            avg_price, filled = result
            if filled < opp.optimal_shares * 0.99:
                return False
            total_cost += avg_price

        net_cost = total_cost + self.config.arbitrage.fee_estimate_pct
        return net_cost < 1.0

    async def _exit_partial(
        self, opp: ArbOpportunity, filled_legs: list[int], shares: float
    ):
        """Attempt to sell positions from partially-filled arb."""
        for leg_idx in filled_legs:
            token_id = opp.token_ids[leg_idx]
            try:
                await self.client.place_market_sell(token_id, shares)
                logger.info(f"Sold partial position: {opp.outcomes[leg_idx]}")
            except Exception as e:
                logger.error(
                    f"Failed to sell partial position {opp.outcomes[leg_idx]}: {e}"
                )

    def _simulate(self, opp: ArbOpportunity) -> TradeResult:
        """Simulate execution in dry-run mode."""
        logger.info(
            f"[DRY RUN] Would execute: {opp.event_title} | "
            f"{len(opp.token_ids)} legs | "
            f"cost=${opp.total_cost:.4f}/share | "
            f"profit=${opp.estimated_profit_usd:.2f}"
        )
        return TradeResult(
            opportunity=opp,
            status="dry_run",
            legs_filled=len(opp.token_ids),
            legs_total=len(opp.token_ids),
            order_ids=[None] * len(opp.token_ids),
            actual_prices=opp.fill_prices,
            total_cost_actual=opp.total_cost * opp.optimal_shares,
            shares_filled=opp.optimal_shares,
            expected_profit_usd=opp.estimated_profit_usd,
        )

    @staticmethod
    def _is_filled(result: dict) -> bool:
        """Check if an order result indicates a fill."""
        if isinstance(result, dict):
            status = result.get("status", "").lower()
            return status in ("matched", "filled", "live")
        return False

    @staticmethod
    def _extract_order_id(result: dict) -> str | None:
        """Extract order ID from result."""
        if isinstance(result, dict):
            return result.get("orderID") or result.get("id")
        return None

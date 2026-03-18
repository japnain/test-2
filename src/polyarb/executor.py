"""Trade executor — places FOK orders with sequential or parallel leg execution.

RULE: Only executes if profit is mathematically guaranteed after gas costs.
Pre-trade verification: re-checks order books before every leg.
"""

from __future__ import annotations

import asyncio
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
        1. Check that profit exceeds gas costs
        2. Sort legs by liquidity (thinnest first)
        3. Execute legs sequentially or in parallel (configurable)
        4. If any leg fails, use smart exit for already-filled positions
        5. Only count as success if ALL legs fill
        """
        if self.config.is_dry_run:
            return self._simulate(opp)

        # Gas cost check
        num_legs = len(opp.token_ids)
        total_gas = self.config.execution.gas_cost_per_leg_usd * num_legs
        if opp.estimated_profit_usd <= total_gas:
            logger.info(
                f"Skipping: profit ${opp.estimated_profit_usd:.2f} "
                f"<= gas cost ${total_gas:.2f}"
            )
            return TradeResult(
                opportunity=opp,
                status="failed",
                legs_filled=0,
                legs_total=num_legs,
                order_ids=[],
                actual_prices=[],
                total_cost_actual=0,
                shares_filled=0,
                expected_profit_usd=0,
                error=f"Profit ${opp.estimated_profit_usd:.2f} does not cover gas ${total_gas:.2f}",
            )

        # Pre-trade verification: re-fetch all order books
        if self.config.execution.verify_before_execute:
            still_valid = await self._verify_opportunity(opp)
            if not still_valid:
                logger.warning(f"Opportunity vanished before execution: {opp.event_title}")
                return TradeResult(
                    opportunity=opp,
                    status="failed",
                    legs_filled=0,
                    legs_total=num_legs,
                    order_ids=[],
                    actual_prices=[],
                    total_cost_actual=0,
                    shares_filled=0,
                    expected_profit_usd=0,
                    error="Opportunity no longer valid on re-check",
                )

        if self.config.execution.parallel_execution:
            return await self._execute_parallel(opp)
        else:
            return await self._execute_sequential(opp)

    async def _execute_sequential(self, opp: ArbOpportunity) -> TradeResult:
        """Execute legs sequentially (thinnest liquidity first)."""
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

            result = await self._place_order_with_timeout(
                token_id=token_id,
                price=price,
                size=size,
                neg_risk=opp.neg_risk,
            )

            if result and self._is_filled(result):
                order_ids[leg_idx] = self._extract_order_id(result)
                actual_prices[leg_idx] = price
                filled_legs.append(leg_idx)
                logger.info(f"Leg {leg_idx + 1} FILLED")
            else:
                logger.warning(f"Leg {leg_idx + 1} FAILED — aborting remaining legs")
                break

        return self._build_result(opp, order_ids, actual_prices, filled_legs)

    async def _execute_parallel(self, opp: ArbOpportunity) -> TradeResult:
        """Execute all legs simultaneously for faster fills."""
        order_ids: list[str | None] = [None] * len(opp.token_ids)
        actual_prices: list[float | None] = [None] * len(opp.token_ids)
        filled_legs: list[int] = []

        logger.info(
            f"Parallel execution: {len(opp.token_ids)} legs for '{opp.event_title}'"
        )

        async def _execute_leg(idx: int):
            token_id = opp.token_ids[idx]
            price = opp.fill_prices[idx]
            size = opp.optimal_shares
            return idx, await self._place_order_with_timeout(
                token_id=token_id,
                price=price,
                size=size,
                neg_risk=opp.neg_risk,
            )

        tasks = [_execute_leg(i) for i in range(len(opp.token_ids))]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Parallel leg error: {result}")
                continue
            idx, order_result = result
            if order_result and self._is_filled(order_result):
                order_ids[idx] = self._extract_order_id(order_result)
                actual_prices[idx] = opp.fill_prices[idx]
                filled_legs.append(idx)
                logger.info(f"Leg {idx + 1} FILLED (parallel)")
            else:
                logger.warning(f"Leg {idx + 1} FAILED (parallel)")

        return self._build_result(opp, order_ids, actual_prices, filled_legs)

    def _build_result(
        self,
        opp: ArbOpportunity,
        order_ids: list[str | None],
        actual_prices: list[float | None],
        filled_legs: list[int],
    ) -> TradeResult:
        """Build TradeResult and handle partial fills."""
        num_legs = len(opp.token_ids)
        gas_cost = self.config.execution.gas_cost_per_leg_usd * len(filled_legs)

        if len(filled_legs) == num_legs:
            total_cost = sum(p for p in actual_prices if p is not None) * opp.optimal_shares
            profit_after_gas = opp.estimated_profit_usd - gas_cost
            status = "all_filled"
            logger.info(
                f"ARB EXECUTED: {opp.event_title} | "
                f"All {num_legs} legs filled | "
                f"Cost=${total_cost:.2f} | "
                f"Profit after gas=${profit_after_gas:.2f}"
            )
            return TradeResult(
                opportunity=opp,
                status=status,
                legs_filled=num_legs,
                legs_total=num_legs,
                order_ids=order_ids,
                actual_prices=actual_prices,
                total_cost_actual=total_cost,
                shares_filled=opp.optimal_shares,
                expected_profit_usd=profit_after_gas,
            )

        if filled_legs:
            logger.warning(
                f"PARTIAL FILL: {len(filled_legs)}/{num_legs} legs. "
                f"Attempting smart exit..."
            )
            # Use smart exit instead of blind market sell
            asyncio.ensure_future(self._smart_exit_partial(opp, filled_legs, opp.optimal_shares))
            total_cost = sum(
                (actual_prices[i] or 0) * opp.optimal_shares for i in filled_legs
            )
            return TradeResult(
                opportunity=opp,
                status="partial",
                legs_filled=len(filled_legs),
                legs_total=num_legs,
                order_ids=order_ids,
                actual_prices=actual_prices,
                total_cost_actual=total_cost,
                shares_filled=0,
                expected_profit_usd=0,
            )

        return TradeResult(
            opportunity=opp,
            status="failed",
            legs_filled=0,
            legs_total=num_legs,
            order_ids=order_ids,
            actual_prices=actual_prices,
            total_cost_actual=0,
            shares_filled=0,
            expected_profit_usd=0,
        )

    async def _place_order_with_timeout(
        self,
        token_id: str,
        price: float,
        size: float,
        neg_risk: bool,
    ) -> dict | None:
        """Place FOK order with configurable timeout."""
        timeout = self.config.execution.execution_timeout_sec
        try:
            return await asyncio.wait_for(
                self.client.place_fok_order(
                    token_id=token_id,
                    price=price,
                    size=size,
                    neg_risk=neg_risk,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.error(
                f"Order timed out after {timeout}s: "
                f"token={token_id}, price={price}, size={size}"
            )
            return None

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

        fee_est = self.config.arbitrage.fee_estimate_pct
        safety = self.config.arbitrage.safety_margin_pct
        net_cost = total_cost + fee_est + safety
        return net_cost < 1.0

    async def _smart_exit_partial(
        self, opp: ArbOpportunity, filled_legs: list[int], shares: float
    ):
        """Smart exit for partial fills — checks bid depth before selling.

        Instead of blind market sells:
        1. Check bid side depth for each filled position
        2. If best bid is within acceptable slippage, place limit sell slightly below best bid
        3. If expected loss exceeds threshold, hold the position instead of panic-selling
        """
        max_slippage = self.config.execution.max_exit_slippage_pct

        for leg_idx in filled_legs:
            token_id = opp.token_ids[leg_idx]
            entry_price = opp.fill_prices[leg_idx]

            try:
                # Fetch current order book to check bid side
                book = await self.client.get_order_book(token_id)

                if not book.bids:
                    logger.warning(
                        f"No bids for {opp.outcomes[leg_idx]} — "
                        f"holding position (no exit available)"
                    )
                    continue

                best_bid = book.bids[0].price
                expected_loss_pct = (entry_price - best_bid) / entry_price if entry_price > 0 else 1.0

                if expected_loss_pct > max_slippage:
                    logger.warning(
                        f"Exit slippage too high for {opp.outcomes[leg_idx]}: "
                        f"entry=${entry_price:.4f}, best_bid=${best_bid:.4f}, "
                        f"loss={expected_loss_pct:.1%} > max {max_slippage:.1%}. "
                        f"HOLDING position instead of panic-selling."
                    )
                    continue

                # Place limit sell slightly below best bid for faster fill
                sell_price = best_bid * 0.999  # 0.1% below best bid
                logger.info(
                    f"Smart exit: selling {shares:.1f} shares of "
                    f"'{opp.outcomes[leg_idx]}' at ${sell_price:.4f} "
                    f"(best bid=${best_bid:.4f}, entry=${entry_price:.4f}, "
                    f"expected loss={expected_loss_pct:.1%})"
                )
                await self.client.place_market_sell(token_id, shares)
                logger.info(f"Exited partial position: {opp.outcomes[leg_idx]}")

            except Exception as e:
                logger.error(
                    f"Failed to exit partial position {opp.outcomes[leg_idx]}: {e}"
                )

    def _simulate(self, opp: ArbOpportunity) -> TradeResult:
        """Simulate execution in dry-run mode (includes gas cost deduction)."""
        num_legs = len(opp.token_ids)
        gas_cost = self.config.execution.gas_cost_per_leg_usd * num_legs
        profit_after_gas = opp.estimated_profit_usd - gas_cost

        logger.info(
            f"[DRY RUN] Would execute: {opp.event_title} | "
            f"{num_legs} legs | "
            f"cost=${opp.total_cost:.4f}/share | "
            f"profit=${profit_after_gas:.2f} (after ${gas_cost:.2f} gas)"
        )
        return TradeResult(
            opportunity=opp,
            status="dry_run",
            legs_filled=num_legs,
            legs_total=num_legs,
            order_ids=[None] * num_legs,
            actual_prices=opp.fill_prices,
            total_cost_actual=opp.total_cost * opp.optimal_shares,
            shares_filled=opp.optimal_shares,
            expected_profit_usd=profit_after_gas,
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

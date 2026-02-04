"""Order placer for executing arbitrage trades with auto-rollback."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, UTC

from src.executor.models import ExecutionResult, LegResult
from src.models import ArbitrageOpportunity, Platform

logger = logging.getLogger(__name__)


class OrderPlacer:
    """Executes arbitrage by placing orders on both platforms with rollback support."""

    def __init__(self, poly_connector, kalshi_connector):
        self.poly = poly_connector
        self.kalshi = kalshi_connector

    async def execute(
        self,
        opp: ArbitrageOpportunity,
        bet_size: float,
    ) -> ExecutionResult:
        """Execute arbitrage by placing both legs in parallel with auto-rollback.

        If one leg succeeds and the other fails, automatically rolls back
        the successful leg by selling at market price.

        Args:
            opp: The arbitrage opportunity to execute
            bet_size: Total dollar amount to bet

        Returns:
            ExecutionResult with status, leg details, and rollback info
        """
        # Get sides from arbitrage calculation
        poly_side = opp.details.get("poly_side", "BUY")
        kalshi_side = opp.details.get("kalshi_side", "no")

        # Calculate dollar amounts for each leg
        poly_amount, kalshi_amount = self._calculate_leg_sizes(
            bet_size,
            opp.yes_price,
            opp.no_price,
            kalshi_side,
        )

        # Get market identifiers
        poly_token_id = opp.details.get("poly_token_id", "")
        kalshi_ticker = opp.details.get("kalshi_ticker", "")

        logger.info(
            f"Executing: poly={poly_side} ${poly_amount:.2f}@{opp.yes_price:.2f}, "
            f"kalshi={kalshi_side} ${kalshi_amount:.2f} (total=${bet_size:.2f})"
        )

        if not poly_token_id or not kalshi_ticker:
            logger.error(f"Missing market IDs: poly={poly_token_id}, kalshi={kalshi_ticker}")
            return ExecutionResult(
                poly_leg=LegResult("polymarket", False, None, 0, 0, 0, "Missing token_id"),
                kalshi_leg=LegResult("kalshi", False, None, 0, 0, 0, "Missing ticker"),
            )

        # Execute both legs in parallel
        poly_task = self._execute_poly_leg(
            poly_token_id, poly_side, opp.yes_price, poly_amount
        )
        kalshi_task = self._execute_kalshi_leg(
            kalshi_ticker, kalshi_side, "buy", opp.no_price, kalshi_amount
        )

        poly_result, kalshi_result = await asyncio.gather(
            poly_task, kalshi_task,
            return_exceptions=True,
        )

        # Convert exceptions to LegResults
        if isinstance(poly_result, Exception):
            poly_result = LegResult("polymarket", False, None, 0, 0, 0, str(poly_result))
        if isinstance(kalshi_result, Exception):
            kalshi_result = LegResult("kalshi", False, None, 0, 0, 0, str(kalshi_result))

        # Check for partial fills and rollback if needed
        rollback_leg = None
        rollback_loss = 0.0

        if poly_result.success and not kalshi_result.success:
            # Poly succeeded, Kalshi failed - rollback Poly
            logger.warning(f"Partial fill: Poly ✅, Kalshi ❌ - rolling back Poly")
            rollback_leg = await self._rollback_poly(poly_token_id, poly_result)
            if rollback_leg.success:
                rollback_loss = poly_result.filled_cost - rollback_leg.filled_cost
                logger.info(f"Rollback successful, spread loss: ${rollback_loss:.2f}")
            else:
                logger.error(f"Rollback FAILED: {rollback_leg.error}")

        elif kalshi_result.success and not poly_result.success:
            # Kalshi succeeded, Poly failed - rollback Kalshi
            logger.warning(f"Partial fill: Kalshi ✅, Poly ❌ - rolling back Kalshi")
            rollback_leg = await self._rollback_kalshi(kalshi_ticker, kalshi_side, kalshi_result)
            if rollback_leg.success:
                rollback_loss = kalshi_result.filled_cost - rollback_leg.filled_cost
                logger.info(f"Rollback successful, spread loss: ${rollback_loss:.2f}")
            else:
                logger.error(f"Rollback FAILED: {rollback_leg.error}")

        # Calculate payout for successful arbitrage
        total_invested = 0.0
        guaranteed_payout = 0.0
        expected_profit = 0.0

        if poly_result.success and kalshi_result.success:
            total_invested = poly_result.filled_cost + kalshi_result.filled_cost
            # Payout is $1 per contract, use minimum of both sides
            min_contracts = min(poly_result.filled_shares, kalshi_result.filled_shares)
            guaranteed_payout = min_contracts
            expected_profit = guaranteed_payout - total_invested
            logger.info(
                f"Arbitrage locked: invested ${total_invested:.2f}, "
                f"payout ${guaranteed_payout:.2f}, profit ${expected_profit:.2f}"
            )

        return ExecutionResult(
            poly_leg=poly_result,
            kalshi_leg=kalshi_result,
            rollback_leg=rollback_leg,
            rollback_loss=rollback_loss,
            total_invested=total_invested,
            guaranteed_payout=guaranteed_payout,
            expected_profit=expected_profit,
        )

    async def _execute_poly_leg(
        self,
        token_id: str,
        side: str,
        price: float,
        dollar_amount: float,
    ) -> LegResult:
        """Execute Polymarket leg.

        Args:
            token_id: CLOB token ID
            side: "BUY" or "SELL"
            price: Price per share (0-1)
            dollar_amount: Dollar amount to spend (API expects dollars for FOK)
        """
        try:
            result = await self.poly.place_order(
                token_id=token_id,
                side=side,
                price=price,
                size=dollar_amount,  # Pass dollars directly for FOK market orders
                order_type="FOK",
            )

            success = result.get("success", False)
            order_id = result.get("orderID") or result.get("order_id")

            # Parse actual filled amounts from response
            # For market orders, matchedAmount is in shares
            filled_shares = float(result.get("matchedAmount", 0)) if success else 0
            avg_price = float(result.get("avgPrice", price)) if success else 0
            filled_cost = filled_shares * avg_price if success else 0

            # Fallback: estimate from dollar_amount if no matchedAmount
            if success and filled_shares == 0:
                filled_shares = dollar_amount / price if price > 0 else 0
                filled_cost = dollar_amount
                avg_price = price

            logger.info(
                f"Poly {side}: {filled_shares:.2f} shares × ${avg_price:.2f} = ${filled_cost:.2f}"
            )

            return LegResult(
                platform="polymarket",
                success=success,
                order_id=order_id,
                filled_shares=filled_shares,
                filled_price=avg_price,
                filled_cost=filled_cost,
                error=result.get("errorMsg") if not success else None,
            )
        except Exception as e:
            logger.error(f"Polymarket order failed: {e}")
            return LegResult("polymarket", False, None, 0, 0, 0, str(e))

    async def _execute_kalshi_leg(
        self,
        ticker: str,
        side: str,
        action: str,
        kalshi_yes_price: float,
        dollar_amount: float,
    ) -> LegResult:
        """Execute Kalshi leg.

        Args:
            ticker: Market ticker
            side: "yes" or "no"
            action: "buy" or "sell"
            kalshi_yes_price: YES price (0-1), we derive NO price from it
            dollar_amount: Dollar amount to spend
        """
        try:
            # Calculate price and contracts based on side
            if side == "no":
                price = 1 - kalshi_yes_price  # NO price
                contracts = int(dollar_amount / price) if price > 0 else 0
                price_cents = int(price * 100)
            else:
                price = kalshi_yes_price
                contracts = int(dollar_amount / price) if price > 0 else 0
                price_cents = int(price * 100)

            contracts = max(1, contracts)

            result = await self.kalshi.place_order(
                ticker=ticker,
                side=side,
                action=action,
                count=contracts,
                price_cents=price_cents,
                time_in_force="fill_or_kill",
            )

            order_id = result.get("order_id")
            status = result.get("status", "")
            success = status in ("filled", "resting") or (order_id is not None and "error" not in result)

            # Parse actual filled amounts
            filled_contracts = contracts if success else 0
            avg_price = price_cents / 100 if success else 0
            filled_cost = filled_contracts * avg_price if success else 0

            logger.info(
                f"Kalshi {action} {side}: {filled_contracts} contracts × ${avg_price:.2f} = ${filled_cost:.2f}"
            )

            return LegResult(
                platform="kalshi",
                success=success,
                order_id=order_id,
                filled_shares=float(filled_contracts),
                filled_price=avg_price,
                filled_cost=filled_cost,
                error=result.get("error") if not success else None,
            )
        except Exception as e:
            logger.error(f"Kalshi order failed: {e}")
            return LegResult("kalshi", False, None, 0, 0, 0, str(e))

    async def _rollback_poly(
        self,
        token_id: str,
        original_leg: LegResult,
    ) -> LegResult:
        """Rollback Polymarket position by selling at market."""
        try:
            # Sell the shares we bought
            result = await self.poly.place_order(
                token_id=token_id,
                side="SELL",
                price=0.01,  # Low price for market sell (FOK will get best available)
                size=original_leg.filled_shares,
                order_type="FOK",
            )

            success = result.get("success", False)
            order_id = result.get("orderID") or result.get("order_id")

            filled_shares = float(result.get("matchedAmount", 0)) if success else 0
            avg_price = float(result.get("avgPrice", 0)) if success else 0
            filled_cost = filled_shares * avg_price

            # Fallback
            if success and filled_shares == 0:
                filled_shares = original_leg.filled_shares
                filled_cost = filled_shares * avg_price if avg_price > 0 else 0

            return LegResult(
                platform="polymarket_rollback",
                success=success,
                order_id=order_id,
                filled_shares=filled_shares,
                filled_price=avg_price,
                filled_cost=filled_cost,
                error=result.get("errorMsg") if not success else None,
            )
        except Exception as e:
            logger.error(f"Poly rollback failed: {e}")
            return LegResult("polymarket_rollback", False, None, 0, 0, 0, str(e))

    async def _rollback_kalshi(
        self,
        ticker: str,
        original_side: str,
        original_leg: LegResult,
    ) -> LegResult:
        """Rollback Kalshi position by selling at market."""
        try:
            contracts = int(original_leg.filled_shares)

            # Sell at low price for market execution
            price_cents = 1  # Minimum price for quick fill

            result = await self.kalshi.place_order(
                ticker=ticker,
                side=original_side,
                action="sell",
                count=contracts,
                price_cents=price_cents,
                time_in_force="fill_or_kill",
            )

            order_id = result.get("order_id")
            status = result.get("status", "")
            success = status in ("filled", "resting") or (order_id is not None and "error" not in result)

            filled_contracts = contracts if success else 0
            avg_price = price_cents / 100 if success else 0
            filled_cost = filled_contracts * avg_price

            return LegResult(
                platform="kalshi_rollback",
                success=success,
                order_id=order_id,
                filled_shares=float(filled_contracts),
                filled_price=avg_price,
                filled_cost=filled_cost,
                error=result.get("error") if not success else None,
            )
        except Exception as e:
            logger.error(f"Kalshi rollback failed: {e}")
            return LegResult("kalshi_rollback", False, None, 0, 0, 0, str(e))

    def _calculate_leg_sizes(
        self,
        bet_size: float,
        poly_price: float,
        kalshi_yes_price: float,
        kalshi_side: str,
    ) -> tuple[float, float]:
        """Calculate dollar amounts for each leg.

        For equal payout arbitrage, we need EQUAL number of contracts on each platform.
        Each contract pays $1 if it wins.
        """
        # Calculate actual cost per contract on Kalshi based on side
        if kalshi_side == "no":
            kalshi_cost = 1 - kalshi_yes_price
        else:
            kalshi_cost = kalshi_yes_price

        # Cost for 1 "set" of contracts (1 Poly + 1 Kalshi)
        cost_per_set = poly_price + kalshi_cost
        if cost_per_set <= 0:
            return bet_size / 2, bet_size / 2

        # How many sets can we buy with our budget?
        num_sets = bet_size / cost_per_set

        # Allocate proportionally to each platform
        poly_amount = round(num_sets * poly_price, 2)
        kalshi_amount = round(num_sets * kalshi_cost, 2)

        # Ensure total matches bet_size
        diff = bet_size - (poly_amount + kalshi_amount)
        if abs(diff) > 0.01:
            poly_amount = round(poly_amount + diff / 2, 2)
            kalshi_amount = round(kalshi_amount + diff / 2, 2)

        return poly_amount, kalshi_amount

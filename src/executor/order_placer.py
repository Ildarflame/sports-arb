"""Order placer for executing arbitrage trades."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, UTC

from src.executor.models import ExecutionResult, LegResult
from src.models import ArbitrageOpportunity, Platform

logger = logging.getLogger(__name__)


class OrderPlacer:
    """Executes arbitrage by placing orders on both platforms."""

    def __init__(self, poly_connector, kalshi_connector):
        self.poly = poly_connector
        self.kalshi = kalshi_connector

    async def execute(
        self,
        opp: ArbitrageOpportunity,
        bet_size: float,
    ) -> ExecutionResult:
        """Execute arbitrage by placing both legs in parallel.

        Args:
            opp: The arbitrage opportunity to execute
            bet_size: Total dollar amount to bet

        Returns:
            ExecutionResult with status and leg details
        """
        # Use explicit sides from arbitrage calculation
        # Polymarket: always BUY the correct token (team_a or team_b)
        poly_side = opp.details.get("poly_side", "BUY")
        # Kalshi: use the pre-calculated side from arbitrage.py
        kalshi_side = opp.details.get("kalshi_side", "no")

        # Calculate sizes for each leg (accounting for Kalshi NO vs YES pricing)
        poly_size, kalshi_size = self._calculate_leg_sizes(
            bet_size,
            opp.yes_price,  # Poly price
            opp.no_price,   # Kalshi YES price (we calculate NO price inside)
            kalshi_side,
        )

        kalshi_action = "buy"

        # Get market identifiers from details
        poly_token_id = opp.details.get("poly_token_id", "")
        kalshi_ticker = opp.details.get("kalshi_ticker", "")

        # Log for debugging
        logger.info(
            f"Order: poly={poly_side} ${poly_size:.2f}@{opp.yes_price:.2f}, "
            f"kalshi={kalshi_side} ${kalshi_size:.2f}@{opp.no_price:.2f} (total=${bet_size:.2f})"
        )

        if not poly_token_id or not kalshi_ticker:
            logger.error(f"Missing market IDs: poly={poly_token_id}, kalshi={kalshi_ticker}")
            return ExecutionResult(
                poly_leg=LegResult("polymarket", False, None, 0, 0, "Missing token_id"),
                kalshi_leg=LegResult("kalshi", False, None, 0, 0, "Missing ticker"),
            )

        # Execute both legs in parallel
        poly_task = self._execute_poly_leg(
            poly_token_id, poly_side, opp.yes_price, poly_size
        )
        kalshi_task = self._execute_kalshi_leg(
            kalshi_ticker, kalshi_side, kalshi_action, opp.no_price, kalshi_size
        )

        poly_result, kalshi_result = await asyncio.gather(
            poly_task, kalshi_task,
            return_exceptions=True,
        )

        # Convert exceptions to LegResults
        if isinstance(poly_result, Exception):
            poly_result = LegResult("polymarket", False, None, 0, 0, str(poly_result))
        if isinstance(kalshi_result, Exception):
            kalshi_result = LegResult("kalshi", False, None, 0, 0, str(kalshi_result))

        return ExecutionResult(poly_leg=poly_result, kalshi_leg=kalshi_result)

    async def _execute_poly_leg(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> LegResult:
        """Execute Polymarket leg."""
        try:
            # Calculate number of contracts from dollar size and price
            contracts = size / price if price > 0 else 0

            result = await self.poly.place_order(
                token_id=token_id,
                side=side,
                price=price,
                size=contracts,
                order_type="FOK",  # Fill or kill for immediate execution
            )

            success = result.get("success", False)
            order_id = result.get("orderID") or result.get("order_id")

            return LegResult(
                platform="polymarket",
                success=success,
                order_id=order_id,
                filled_amount=size if success else 0,
                filled_price=price if success else 0,
                error=result.get("errorMsg") if not success else None,
            )
        except Exception as e:
            logger.error(f"Polymarket order failed: {e}")
            return LegResult("polymarket", False, None, 0, 0, str(e))

    async def _execute_kalshi_leg(
        self,
        ticker: str,
        side: str,
        action: str,
        price: float,
        size: float,
    ) -> LegResult:
        """Execute Kalshi leg."""
        try:
            # Calculate number of contracts (Kalshi uses integer contracts)
            # Each contract pays $1, so contracts = size / (1 - price) for NO side
            if side == "no":
                contracts = int(size / (1 - price)) if price < 1 else 0
                price_cents = int((1 - price) * 100)  # NO price
            else:
                contracts = int(size / price) if price > 0 else 0
                price_cents = int(price * 100)

            contracts = max(1, contracts)  # At least 1 contract

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
            success = status in ("filled", "resting") or order_id is not None

            return LegResult(
                platform="kalshi",
                success=success,
                order_id=order_id,
                filled_amount=size if success else 0,
                filled_price=price if success else 0,
                error=result.get("error") if not success else None,
            )
        except Exception as e:
            logger.error(f"Kalshi order failed: {e}")
            return LegResult("kalshi", False, None, 0, 0, str(e))

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

        poly_price: cost to buy 1 Poly contract (e.g., 0.48)
        kalshi_yes_price: Kalshi YES price (we derive NO price from it)
        kalshi_side: "yes" or "no" - determines which price to use
        """
        # Calculate actual cost per contract on Kalshi based on side
        if kalshi_side == "no":
            kalshi_cost = 1 - kalshi_yes_price  # NO price = 1 - YES price
        else:
            kalshi_cost = kalshi_yes_price

        # Cost for 1 "set" of contracts (1 Poly + 1 Kalshi)
        cost_per_set = poly_price + kalshi_cost
        if cost_per_set <= 0:
            return bet_size / 2, bet_size / 2

        # How many sets can we buy with our budget?
        num_sets = bet_size / cost_per_set

        # Allocate proportionally to each platform
        poly_size = round(num_sets * poly_price, 2)
        kalshi_size = round(num_sets * kalshi_cost, 2)

        # Ensure total matches bet_size (rounding fix)
        diff = bet_size - (poly_size + kalshi_size)
        if abs(diff) > 0.01:
            # Split difference
            poly_size = round(poly_size + diff / 2, 2)
            kalshi_size = round(kalshi_size + diff / 2, 2)

        return poly_size, kalshi_size

"""Main executor that orchestrates arbitrage execution."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, UTC

from src.executor.models import ExecutionResult, ExecutionStatus, OpenPosition
from src.executor.order_placer import OrderPlacer
from src.executor.position_manager import PositionManager
from src.executor.risk_manager import RiskManager
from src.executor.telegram_bot import TelegramNotifier
from src.models import ArbitrageOpportunity

logger = logging.getLogger(__name__)


class Executor:
    """Orchestrates arbitrage detection and execution."""

    def __init__(
        self,
        risk_manager: RiskManager,
        order_placer: OrderPlacer,
        position_manager: PositionManager,
        telegram: TelegramNotifier,
        poly_connector,
        kalshi_connector,
    ):
        self.risk = risk_manager
        self.placer = order_placer
        self.positions = position_manager
        self.telegram = telegram
        self.poly = poly_connector
        self.kalshi = kalshi_connector

    async def try_execute(self, opp: ArbitrageOpportunity) -> ExecutionResult | None:
        """Attempt to execute an arbitrage opportunity.

        Returns ExecutionResult if executed, None if skipped.
        """
        # Get current balances
        try:
            poly_balance = await self.poly.get_balance()
            kalshi_balance = await self.kalshi.get_balance()
        except Exception as e:
            logger.error(f"Failed to fetch balances: {e}")
            return None

        # Run risk checks
        logger.info(f"EXEC CHECKING: {opp.event_title} ROI={opp.roi_after_fees}% poly={poly_balance:.2f} kalshi={kalshi_balance:.2f} | poly_token={opp.details.get('poly_token_id', 'NONE')[:20] if opp.details.get('poly_token_id') else 'NONE'} kalshi_ticker={opp.details.get('kalshi_ticker', 'NONE')}")
        check = self.risk.check_opportunity(opp, poly_balance, kalshi_balance)
        if not check.passed:
            logger.info(f"RISK REJECTED: {opp.event_title} | {check.reason}")
            return None

        # Calculate bet size
        bet_size = self.risk.calculate_bet_size(opp, poly_balance, kalshi_balance)
        if bet_size <= 0:
            logger.debug(f"Bet size too small for {opp.event_title}")
            return None

        # Use kalshi_ticker as unique key for deduplication (more reliable than team names)
        kalshi_ticker = opp.details.get("kalshi_ticker", "")
        dedup_key = kalshi_ticker if kalshi_ticker else f"{opp.team_a}:{opp.team_b}"

        # CRITICAL: Add to open positions BEFORE executing to prevent duplicates
        # This prevents race conditions where multiple scans try to execute same opportunity
        self.risk.add_open_position(dedup_key)

        logger.info(f"EXECUTING: {opp.event_title} | ROI={opp.roi_after_fees}% | bet=${bet_size} | key={dedup_key}")

        # Execute the trade
        result = await self.placer.execute(opp, bet_size)

        # Calculate expected profit
        expected_profit = bet_size * (opp.roi_after_fees / 100)

        # Handle result
        if result.status == ExecutionStatus.SUCCESS:
            # Save position with retry for database locks
            position = self._create_position(opp, result, bet_size)
            for attempt in range(3):
                try:
                    await self.positions.save_position(position)
                    break
                except Exception as e:
                    if attempt < 2:
                        logger.warning(f"Retry {attempt + 1}/3 saving position: {e}")
                        import asyncio
                        await asyncio.sleep(0.5 * (attempt + 1))
                    else:
                        logger.error(f"Failed to save position after 3 attempts: {e}")
            self.risk.record_trade(dedup_key)

            logger.info(f"SUCCESS: {opp.event_title} | Expected profit: ${expected_profit:.2f}")

        elif result.status == ExecutionStatus.PARTIAL:
            # Partial fill - save position with partial status
            position = self._create_position(opp, result, bet_size)
            position.status = "partial"
            for attempt in range(3):
                try:
                    await self.positions.save_position(position)
                    break
                except Exception as e:
                    if attempt < 2:
                        logger.warning(f"Retry {attempt + 1}/3 saving partial position: {e}")
                        import asyncio
                        await asyncio.sleep(0.5 * (attempt + 1))
                    else:
                        logger.error(f"Failed to save partial position after 3 attempts: {e}")

            logger.warning(f"PARTIAL: {opp.event_title} - needs attention!")

        else:
            # Both legs failed - remove from open positions so it can be retried
            self.risk.remove_open_position(dedup_key)
            logger.warning(f"FAILED: {opp.event_title} - both legs failed, will retry")

        # Send notification
        try:
            new_poly_balance = await self.poly.get_balance()
            new_kalshi_balance = await self.kalshi.get_balance()
        except Exception:
            new_poly_balance = poly_balance
            new_kalshi_balance = kalshi_balance

        await self.telegram.notify_execution(
            result=result,
            event_title=opp.event_title,
            roi=opp.roi_after_fees,
            profit=expected_profit,
            poly_balance=new_poly_balance,
            kalshi_balance=new_kalshi_balance,
        )

        # Check if we hit kill switch conditions
        stats = self.risk.get_stats()
        if stats["daily_pnl"] <= -self.risk.max_daily_loss:
            self.risk.enabled = False
            await self.telegram.notify_kill_switch(
                reason="Daily loss limit reached",
                daily_trades=stats["daily_trades"],
                pnl=stats["daily_pnl"],
            )

        return result

    def _create_position(
        self,
        opp: ArbitrageOpportunity,
        result: ExecutionResult,
        bet_size: float,
    ) -> OpenPosition:
        """Create position record from execution result."""
        return OpenPosition(
            id=str(uuid.uuid4()),
            event_title=opp.event_title,
            team_a=opp.team_a,
            team_b=opp.team_b,
            poly_side="YES" if opp.platform_buy_yes.value == "polymarket" else "NO",
            poly_amount=result.poly_leg.filled_cost,
            poly_contracts=result.poly_leg.filled_shares,
            poly_avg_price=result.poly_leg.filled_price,
            poly_order_id=result.poly_leg.order_id or "",
            kalshi_side="no" if opp.platform_buy_no.value == "kalshi" else "yes",
            kalshi_amount=result.kalshi_leg.filled_cost,
            kalshi_contracts=int(result.kalshi_leg.filled_shares),
            kalshi_avg_price=result.kalshi_leg.filled_price,
            kalshi_order_id=result.kalshi_leg.order_id or "",
            arb_type=opp.details.get("arb_type", "yes_no"),
            expected_roi=opp.roi_after_fees,
            opened_at=datetime.now(UTC),
            status="open",
        )

    async def check_settlements(self) -> None:
        """Check open positions for settlement."""
        positions = await self.positions.get_open_positions()

        for pos in positions:
            # TODO: Check if event has settled on both platforms
            # For now, this is a placeholder for manual settlement
            logger.debug(f"Checking settlement for: {pos.event_title}")

    async def send_daily_summary(self) -> None:
        """Send end-of-day summary via Telegram."""
        stats = await self.positions.get_daily_stats()

        try:
            poly_balance = await self.poly.get_balance()
            kalshi_balance = await self.kalshi.get_balance()
        except Exception:
            poly_balance = 0
            kalshi_balance = 0

        await self.telegram.notify_daily_summary(
            trades=stats["trades"],
            successful=stats["settled"],
            partial=stats["partial"],
            pnl=stats["pnl"],
            poly_balance=poly_balance,
            kalshi_balance=kalshi_balance,
        )

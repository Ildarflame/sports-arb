"""Risk manager for pre-trade validation."""

from __future__ import annotations

import logging
from datetime import date

from src.executor.models import RiskCheckResult
from src.models import ArbitrageOpportunity

logger = logging.getLogger(__name__)


class RiskManager:
    """Validates opportunities against risk limits before execution."""

    def __init__(
        self,
        min_bet: float = 1.0,
        max_bet: float = 2.0,
        min_roi: float = 1.0,
        max_roi: float = 50.0,
        max_daily_trades: int = 50,
        max_daily_loss: float = 5.0,
        min_platform_balance: float = 1.0,
    ):
        self.min_bet = min_bet
        self.max_bet = max_bet
        self.min_roi = min_roi
        self.max_roi = max_roi
        self.max_daily_trades = max_daily_trades
        self.max_daily_loss = max_daily_loss
        self.min_platform_balance = min_platform_balance

        # Runtime state
        self.enabled = True
        self._daily_trades = 0
        self._daily_pnl = 0.0
        self._current_date = date.today()
        self._open_positions: set[str] = set()  # event keys with open positions

    def _reset_daily_if_needed(self) -> None:
        """Reset daily counters if date changed."""
        today = date.today()
        if today != self._current_date:
            self._daily_trades = 0
            self._daily_pnl = 0.0
            self._current_date = today
            logger.info("Daily counters reset for new day")

    def check_opportunity(
        self,
        opp: ArbitrageOpportunity,
        poly_balance: float,
        kalshi_balance: float,
    ) -> RiskCheckResult:
        """Run all risk checks on opportunity.

        Returns RiskCheckResult with passed=True if all checks pass,
        or passed=False with reason describing first failed check.
        """
        self._reset_daily_if_needed()

        # Skip 3-way arbs (not yet supported for execution)
        arb_type = opp.details.get("arb_type", "")
        if arb_type == "3way":
            return RiskCheckResult(False, "3-way arbs not yet supported for auto-execution")

        # Skip if missing trading identifiers
        if not opp.details.get("poly_token_id"):
            return RiskCheckResult(False, "Missing poly_token_id")
        if not opp.details.get("kalshi_ticker"):
            return RiskCheckResult(False, "Missing kalshi_ticker")
        if not opp.details.get("poly_side"):
            return RiskCheckResult(False, "Missing poly_side (BUY expected)")
        if not opp.details.get("kalshi_side"):
            return RiskCheckResult(False, "Missing kalshi_side (yes/no expected)")

        # 1. Kill switch
        if not self.enabled:
            return RiskCheckResult(False, "Kill switch is OFF - trading disabled")

        # 2. Balance checks
        if poly_balance < self.min_platform_balance:
            return RiskCheckResult(False, f"Polymarket balance too low: ${poly_balance:.2f}")
        if kalshi_balance < self.min_platform_balance:
            return RiskCheckResult(False, f"Kalshi balance too low: ${kalshi_balance:.2f}")

        # 3. ROI checks
        if opp.roi_after_fees < self.min_roi:
            return RiskCheckResult(False, f"ROI too low: {opp.roi_after_fees:.2f}% < {self.min_roi}%")
        if opp.roi_after_fees > self.max_roi:
            return RiskCheckResult(False, f"Suspicious ROI: {opp.roi_after_fees:.2f}% > {self.max_roi}%")

        # 4. Daily limits
        if self._daily_trades >= self.max_daily_trades:
            return RiskCheckResult(False, f"Daily trade limit reached: {self._daily_trades}/{self.max_daily_trades}")
        if self._daily_pnl <= -self.max_daily_loss:
            return RiskCheckResult(False, f"Daily loss limit reached: ${abs(self._daily_pnl):.2f}")

        # 5. Duplicate position check - use kalshi_ticker as unique key (more reliable)
        kalshi_ticker = opp.details.get("kalshi_ticker", "")
        event_key = kalshi_ticker.lower() if kalshi_ticker else f"{opp.team_a}:{opp.team_b}".lower()
        if event_key in self._open_positions:
            return RiskCheckResult(False, f"Already have open position on {opp.event_title} ({event_key})")

        # 6. Confidence check - require HIGH confidence for all arbs
        # This ensures good liquidity and reliable prices
        confidence = opp.details.get("confidence", "low")
        if confidence != "high":
            return RiskCheckResult(False, f"Requires high confidence (got {confidence})")

        # 7. Executable bid/ask required
        if not opp.details.get("executable"):
            return RiskCheckResult(False, "Requires executable bid/ask prices")

        return RiskCheckResult(True, None)

    def calculate_bet_size(
        self,
        opp: ArbitrageOpportunity,
        poly_balance: float,
        kalshi_balance: float,
    ) -> float:
        """Calculate optimal bet size within limits.

        Uses conservative sizing: min of max_bet and available balance.
        """
        # Can't bet more than we have on either platform
        max_by_balance = min(poly_balance, kalshi_balance)

        # Apply configured limits
        bet = min(self.max_bet, max_by_balance)
        bet = max(bet, self.min_bet)

        # Final sanity check
        if bet > max_by_balance:
            bet = max_by_balance

        return round(bet, 2)

    def record_trade(self, event_key: str, pnl: float = 0.0) -> None:
        """Record completed trade for daily tracking."""
        self._daily_trades += 1
        self._daily_pnl += pnl
        logger.info(f"Trade recorded: daily={self._daily_trades}, pnl=${self._daily_pnl:.2f}")

    def add_open_position(self, event_key: str) -> None:
        """Track open position to prevent duplicates."""
        self._open_positions.add(event_key.lower())

    def remove_open_position(self, event_key: str) -> None:
        """Remove settled position from tracking."""
        self._open_positions.discard(event_key.lower())

    def get_stats(self) -> dict:
        """Return current risk manager state."""
        return {
            "enabled": self.enabled,
            "daily_trades": self._daily_trades,
            "daily_pnl": self._daily_pnl,
            "open_positions": len(self._open_positions),
            "limits": {
                "min_bet": self.min_bet,
                "max_bet": self.max_bet,
                "min_roi": self.min_roi,
                "max_daily_trades": self.max_daily_trades,
                "max_daily_loss": self.max_daily_loss,
            },
        }

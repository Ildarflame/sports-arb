"""Data models for executor module."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum


class ExecutionStatus(Enum):
    """Status of arbitrage execution."""
    SUCCESS = "success"      # Both legs filled
    PARTIAL = "partial"      # One leg filled, one failed
    FAILED = "failed"        # Both legs failed


@dataclass
class LegResult:
    """Result of executing one leg of the arbitrage."""
    platform: str           # "polymarket" or "kalshi"
    success: bool
    order_id: str | None
    filled_amount: float    # Dollar amount filled
    filled_price: float     # Average fill price
    error: str | None


@dataclass
class ExecutionResult:
    """Result of executing full arbitrage (both legs)."""
    poly_leg: LegResult
    kalshi_leg: LegResult
    executed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def status(self) -> ExecutionStatus:
        """Determine execution status from leg results."""
        if self.poly_leg.success and self.kalshi_leg.success:
            return ExecutionStatus.SUCCESS
        elif self.poly_leg.success or self.kalshi_leg.success:
            return ExecutionStatus.PARTIAL
        else:
            return ExecutionStatus.FAILED


@dataclass
class OpenPosition:
    """Tracks an open arbitrage position until settlement."""
    id: str
    event_title: str
    team_a: str
    team_b: str

    # Polymarket leg
    poly_side: str          # "YES" or "NO"
    poly_amount: float      # Dollar amount spent
    poly_contracts: float   # Number of contracts
    poly_avg_price: float

    # Kalshi leg
    kalshi_side: str        # "yes" or "no"
    kalshi_amount: float
    kalshi_contracts: int
    kalshi_avg_price: float

    # Metadata
    arb_type: str           # "yes_no", "cross_team", "3way"
    expected_roi: float

    # Fields with defaults (must come after non-default fields)
    poly_order_id: str = ""
    kalshi_order_id: str = ""
    opened_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: str = "open"    # "open", "settled", "partial"

    # Settlement (filled after match ends)
    settled_at: datetime | None = None
    actual_pnl: float | None = None
    winning_side: str | None = None


@dataclass
class RiskCheckResult:
    """Result of pre-trade risk check."""
    passed: bool
    reason: str | None = None

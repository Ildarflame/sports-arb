"""Tests for executor data models."""

from datetime import datetime, UTC

from src.executor.models import (
    ExecutionResult,
    ExecutionStatus,
    LegResult,
    OpenPosition,
    RiskCheckResult,
)


def test_leg_result_success():
    """LegResult can represent successful order."""
    leg = LegResult(
        platform="polymarket",
        success=True,
        order_id="abc123",
        filled_shares=2.94,
        filled_price=0.51,
        filled_cost=1.50,
    )
    assert leg.success
    assert leg.order_id == "abc123"
    assert leg.filled_shares == 2.94
    assert leg.filled_cost == 1.50


def test_leg_result_failure():
    """LegResult can represent failed order."""
    leg = LegResult(
        platform="kalshi",
        success=False,
        order_id=None,
        filled_shares=0,
        filled_price=0,
        filled_cost=0,
        error="Insufficient balance",
    )
    assert not leg.success
    assert leg.error == "Insufficient balance"


def test_execution_result_both_filled():
    """ExecutionResult determines SUCCESS when both legs fill."""
    poly = LegResult("polymarket", True, "p1", 2.0, 0.51, 1.02)
    kalshi = LegResult("kalshi", True, "k1", 2.0, 0.48, 0.96)

    result = ExecutionResult(poly_leg=poly, kalshi_leg=kalshi)
    assert result.status == ExecutionStatus.SUCCESS


def test_execution_result_partial():
    """ExecutionResult determines PARTIAL when one leg fails (no rollback)."""
    poly = LegResult("polymarket", True, "p1", 2.0, 0.51, 1.02)
    kalshi = LegResult("kalshi", False, None, 0, 0, 0, "Failed")

    result = ExecutionResult(poly_leg=poly, kalshi_leg=kalshi)
    assert result.status == ExecutionStatus.PARTIAL


def test_execution_result_rolled_back():
    """ExecutionResult determines ROLLED_BACK when rollback succeeds."""
    poly = LegResult("polymarket", True, "p1", 2.0, 0.51, 1.02)
    kalshi = LegResult("kalshi", False, None, 0, 0, 0, "Failed")
    rollback = LegResult("polymarket_rollback", True, "rb1", 2.0, 0.49, 0.98)

    result = ExecutionResult(
        poly_leg=poly,
        kalshi_leg=kalshi,
        rollback_leg=rollback,
        rollback_loss=0.04,
    )
    assert result.status == ExecutionStatus.ROLLED_BACK


def test_execution_result_rollback_failed():
    """ExecutionResult determines ROLLBACK_FAILED when rollback fails."""
    poly = LegResult("polymarket", True, "p1", 2.0, 0.51, 1.02)
    kalshi = LegResult("kalshi", False, None, 0, 0, 0, "Failed")
    rollback = LegResult("polymarket_rollback", False, None, 0, 0, 0, "No liquidity")

    result = ExecutionResult(
        poly_leg=poly,
        kalshi_leg=kalshi,
        rollback_leg=rollback,
    )
    assert result.status == ExecutionStatus.ROLLBACK_FAILED


def test_execution_result_both_failed():
    """ExecutionResult determines FAILED when both legs fail."""
    poly = LegResult("polymarket", False, None, 0, 0, 0, "Error 1")
    kalshi = LegResult("kalshi", False, None, 0, 0, 0, "Error 2")

    result = ExecutionResult(poly_leg=poly, kalshi_leg=kalshi)
    assert result.status == ExecutionStatus.FAILED


def test_open_position():
    """OpenPosition tracks arbitrage position."""
    pos = OpenPosition(
        id="pos_123",
        event_title="Lakers vs Celtics",
        team_a="Lakers",
        team_b="Celtics",
        poly_side="YES",
        poly_amount=1.02,
        poly_contracts=2.0,
        poly_avg_price=0.51,
        kalshi_side="no",
        kalshi_amount=0.98,
        kalshi_contracts=2,
        kalshi_avg_price=0.49,
        arb_type="yes_no",
        expected_roi=2.1,
        opened_at=datetime.now(UTC),
        status="open",
    )
    assert pos.status == "open"
    assert pos.poly_side == "YES"


def test_risk_check_result():
    """RiskCheckResult indicates pass/fail with reason."""
    passed = RiskCheckResult(passed=True, reason=None)
    assert passed.passed

    failed = RiskCheckResult(passed=False, reason="Daily loss limit reached")
    assert not failed.passed
    assert failed.reason == "Daily loss limit reached"

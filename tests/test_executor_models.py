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
        filled_amount=1.5,
        filled_price=0.52,
        error=None,
    )
    assert leg.success
    assert leg.order_id == "abc123"


def test_leg_result_failure():
    """LegResult can represent failed order."""
    leg = LegResult(
        platform="kalshi",
        success=False,
        order_id=None,
        filled_amount=0,
        filled_price=0,
        error="Insufficient balance",
    )
    assert not leg.success
    assert leg.error == "Insufficient balance"


def test_execution_result_both_filled():
    """ExecutionResult determines SUCCESS when both legs fill."""
    poly = LegResult("polymarket", True, "p1", 1.0, 0.51, None)
    kalshi = LegResult("kalshi", True, "k1", 1.0, 0.48, None)

    result = ExecutionResult(poly_leg=poly, kalshi_leg=kalshi)
    assert result.status == ExecutionStatus.SUCCESS


def test_execution_result_partial():
    """ExecutionResult determines PARTIAL when one leg fails."""
    poly = LegResult("polymarket", True, "p1", 1.0, 0.51, None)
    kalshi = LegResult("kalshi", False, None, 0, 0, "Failed")

    result = ExecutionResult(poly_leg=poly, kalshi_leg=kalshi)
    assert result.status == ExecutionStatus.PARTIAL


def test_execution_result_both_failed():
    """ExecutionResult determines FAILED when both legs fail."""
    poly = LegResult("polymarket", False, None, 0, 0, "Error 1")
    kalshi = LegResult("kalshi", False, None, 0, 0, "Error 2")

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

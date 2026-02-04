"""Tests for main executor."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, UTC

from src.executor.executor import Executor
from src.executor.models import ExecutionStatus, LegResult, ExecutionResult
from src.models import ArbitrageOpportunity, Platform


@pytest.fixture
def mock_components():
    """Create mock executor components."""
    return {
        "risk_manager": MagicMock(),
        "order_placer": AsyncMock(),
        "position_manager": AsyncMock(),
        "telegram": AsyncMock(),
        "poly_connector": AsyncMock(),
        "kalshi_connector": AsyncMock(),
    }


@pytest.fixture
def sample_opportunity():
    """Create sample opportunity."""
    return ArbitrageOpportunity(
        event_title="Lakers vs Celtics",
        team_a="Lakers",
        team_b="Celtics",
        platform_buy_yes=Platform.POLYMARKET,
        platform_buy_no=Platform.KALSHI,
        yes_price=0.51,
        no_price=0.48,
        total_cost=0.99,
        profit_pct=1.0,
        roi_after_fees=2.5,
        found_at=datetime.now(UTC),
        details={
            "poly_token_id": "token_123",
            "kalshi_ticker": "KXNBA-123",
            "poly_side": "BUY",
            "kalshi_side": "no",
        },
    )


@pytest.mark.asyncio
async def test_executor_skips_when_disabled(mock_components, sample_opportunity):
    """Should skip execution when disabled."""
    mock_components["risk_manager"].enabled = False
    mock_components["risk_manager"].check_opportunity.return_value = MagicMock(
        passed=False, reason="Kill switch is OFF"
    )
    # Must set balance returns so logger can format them
    mock_components["poly_connector"].get_balance.return_value = 10.0
    mock_components["kalshi_connector"].get_balance.return_value = 10.0

    executor = Executor(**mock_components)
    result = await executor.try_execute(sample_opportunity)

    assert result is None
    mock_components["order_placer"].execute.assert_not_called()


@pytest.mark.asyncio
async def test_executor_skips_failed_risk_check(mock_components, sample_opportunity):
    """Should skip execution when risk check fails."""
    mock_components["risk_manager"].enabled = True
    mock_components["risk_manager"].check_opportunity.return_value = MagicMock(
        passed=False, reason="ROI too low"
    )
    mock_components["poly_connector"].get_balance.return_value = 10.0
    mock_components["kalshi_connector"].get_balance.return_value = 10.0

    executor = Executor(**mock_components)
    result = await executor.try_execute(sample_opportunity)

    assert result is None


@pytest.mark.asyncio
async def test_executor_executes_valid_opportunity(mock_components, sample_opportunity):
    """Should execute valid opportunity."""
    mock_components["risk_manager"].enabled = True
    mock_components["risk_manager"].check_opportunity.return_value = MagicMock(passed=True)
    mock_components["risk_manager"].calculate_bet_size.return_value = 2.0
    mock_components["risk_manager"].max_daily_loss = 5.0
    mock_components["risk_manager"].get_stats.return_value = {"daily_pnl": 0, "daily_trades": 1}
    mock_components["poly_connector"].get_balance.return_value = 10.0
    mock_components["kalshi_connector"].get_balance.return_value = 10.0

    # LegResult(platform, success, order_id, filled_shares, filled_price, filled_cost)
    poly_leg = LegResult("polymarket", True, "p1", 2.0, 0.51, 1.02)
    kalshi_leg = LegResult("kalshi", True, "k1", 2.0, 0.49, 0.98)
    mock_components["order_placer"].execute.return_value = ExecutionResult(
        poly_leg=poly_leg, kalshi_leg=kalshi_leg
    )

    executor = Executor(**mock_components)
    result = await executor.try_execute(sample_opportunity)

    assert result is not None
    assert result.status == ExecutionStatus.SUCCESS
    mock_components["telegram"].notify_execution.assert_called_once()
    mock_components["position_manager"].save_position.assert_called_once()

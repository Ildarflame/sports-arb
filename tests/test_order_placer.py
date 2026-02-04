"""Tests for order placer."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, UTC

from src.executor.order_placer import OrderPlacer
from src.executor.models import ExecutionStatus
from src.models import ArbitrageOpportunity, Platform


@pytest.fixture
def sample_opportunity():
    """Create sample arbitrage opportunity."""
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
            "poly_url": "https://polymarket.com/...",
            "kalshi_url": "https://kalshi.com/...",
            "poly_side": "BUY",
            "kalshi_side": "no",
        },
    )


@pytest.mark.asyncio
async def test_execute_both_success(sample_opportunity):
    """Should return SUCCESS when both legs fill."""
    mock_poly = AsyncMock()
    mock_poly.place_order.return_value = {
        "success": True,
        "orderID": "poly_123",
        "matchedAmount": 5.0,  # Filled shares
        "avgPrice": 0.51,
    }

    mock_kalshi = AsyncMock()
    mock_kalshi.place_order.return_value = {
        "order_id": "kalshi_456",
        "status": "filled",
        "count_filled": 2,
    }

    placer = OrderPlacer(poly_connector=mock_poly, kalshi_connector=mock_kalshi)
    # Use $3 bet to ensure both legs are >= $1 (with 0.51/0.49 split)
    result = await placer.execute(sample_opportunity, bet_size=5.0)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.poly_leg.success
    assert result.kalshi_leg.success


@pytest.mark.asyncio
async def test_execute_poly_fails(sample_opportunity):
    """Should return ROLLED_BACK when Poly fails and Kalshi is rolled back."""
    mock_poly = AsyncMock()
    mock_poly.place_order.side_effect = Exception("Insufficient balance")

    mock_kalshi = AsyncMock()
    mock_kalshi.place_order.return_value = {
        "order_id": "kalshi_456",
        "status": "filled",
    }

    placer = OrderPlacer(poly_connector=mock_poly, kalshi_connector=mock_kalshi)
    # Use $3 bet to ensure both legs are >= $1
    result = await placer.execute(sample_opportunity, bet_size=5.0)

    # With auto-rollback, partial fills get rolled back
    assert result.status == ExecutionStatus.ROLLED_BACK
    assert not result.poly_leg.success
    assert result.kalshi_leg.success
    assert result.rollback_leg is not None
    assert result.rollback_leg.success


@pytest.mark.asyncio
async def test_execute_kalshi_fails(sample_opportunity):
    """Should return ROLLED_BACK when Kalshi fails and Poly is rolled back."""
    mock_poly = AsyncMock()
    mock_poly.place_order.return_value = {
        "success": True,
        "orderID": "poly_123",
        "matchedAmount": 5.0,  # Filled shares
        "avgPrice": 0.51,
    }

    mock_kalshi = AsyncMock()
    mock_kalshi.place_order.side_effect = Exception("Market closed")

    placer = OrderPlacer(poly_connector=mock_poly, kalshi_connector=mock_kalshi)
    # Use $3 bet to ensure both legs are >= $1
    result = await placer.execute(sample_opportunity, bet_size=5.0)

    # With auto-rollback, partial fills get rolled back
    assert result.status == ExecutionStatus.ROLLED_BACK
    assert result.poly_leg.success
    assert not result.kalshi_leg.success
    assert result.rollback_leg is not None
    assert result.rollback_leg.success


@pytest.mark.asyncio
async def test_execute_both_fail(sample_opportunity):
    """Should return FAILED when both legs fail."""
    mock_poly = AsyncMock()
    mock_poly.place_order.side_effect = Exception("Poly error")

    mock_kalshi = AsyncMock()
    mock_kalshi.place_order.side_effect = Exception("Kalshi error")

    placer = OrderPlacer(poly_connector=mock_poly, kalshi_connector=mock_kalshi)
    # Use $3 bet to ensure both legs are >= $1
    result = await placer.execute(sample_opportunity, bet_size=5.0)

    assert result.status == ExecutionStatus.FAILED
    assert not result.poly_leg.success
    assert not result.kalshi_leg.success


@pytest.mark.asyncio
async def test_poly_fok_no_fill_treated_as_failure(sample_opportunity):
    """FOK order with success=True but matchedAmount=0 should be treated as failure."""
    mock_poly = AsyncMock()
    mock_poly.place_order.return_value = {
        "success": True,  # API says success
        "orderID": "poly_123",
        # BUT matchedAmount=0 or missing means no fill
    }

    mock_kalshi = AsyncMock()
    mock_kalshi.place_order.return_value = {
        "order_id": "kalshi_456",
        "status": "filled",
        "count_filled": 4,
    }

    placer = OrderPlacer(poly_connector=mock_poly, kalshi_connector=mock_kalshi)
    result = await placer.execute(sample_opportunity, bet_size=5.0)

    # Poly should be treated as failed (no fill), Kalshi should be rolled back
    assert result.status == ExecutionStatus.ROLLED_BACK
    assert not result.poly_leg.success  # Override to False due to no matchedAmount
    assert result.kalshi_leg.success
    assert result.rollback_leg is not None


def test_calculate_leg_sizes():
    """Should calculate proportional leg sizes for equal contracts."""
    placer = OrderPlacer(poly_connector=MagicMock(), kalshi_connector=MagicMock())

    # For kalshi_side="no", kalshi_cost = 1 - kalshi_yes_price
    poly_size, kalshi_size = placer._calculate_leg_sizes(
        bet_size=2.0,
        poly_price=0.51,
        kalshi_yes_price=0.52,  # Kalshi YES price
        kalshi_side="no",       # We buy NO, so cost = 1 - 0.52 = 0.48
    )

    # Total should equal bet_size
    assert abs((poly_size + kalshi_size) - 2.0) < 0.01
    # With equal contracts strategy: sizes proportional to per-contract cost
    # Poly cost per contract: 0.51, Kalshi NO cost: 0.48
    assert poly_size > kalshi_size  # Higher price = more dollars allocated

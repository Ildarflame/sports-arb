"""Tests for position manager."""

import pytest
from datetime import datetime, UTC

from src.executor.position_manager import PositionManager
from src.executor.models import OpenPosition


@pytest.fixture
async def position_manager(tmp_path):
    """Create position manager with temp database."""
    db_path = str(tmp_path / "test.db")
    pm = PositionManager(db_path)
    await pm.connect()
    yield pm
    await pm.close()


@pytest.fixture
def sample_position():
    """Create sample open position."""
    return OpenPosition(
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
        poly_order_id="poly_abc",
        kalshi_order_id="kalshi_xyz",
        opened_at=datetime.now(UTC),
        status="open",
    )


@pytest.mark.asyncio
async def test_save_position(position_manager, sample_position):
    """Should save position to database."""
    await position_manager.save_position(sample_position)

    positions = await position_manager.get_open_positions()
    assert len(positions) == 1
    assert positions[0].id == "pos_123"


@pytest.mark.asyncio
async def test_get_open_positions(position_manager, sample_position):
    """Should return only open positions."""
    await position_manager.save_position(sample_position)

    # Create and save a settled position
    settled = OpenPosition(
        id="pos_456",
        event_title="Heat vs Bulls",
        team_a="Heat",
        team_b="Bulls",
        poly_side="YES",
        poly_amount=1.0,
        poly_contracts=2.0,
        poly_avg_price=0.50,
        kalshi_side="no",
        kalshi_amount=1.0,
        kalshi_contracts=2,
        kalshi_avg_price=0.50,
        arb_type="yes_no",
        expected_roi=2.0,
        status="settled",
    )
    await position_manager.save_position(settled)

    positions = await position_manager.get_open_positions()
    assert len(positions) == 1
    assert positions[0].id == "pos_123"


@pytest.mark.asyncio
async def test_settle_position(position_manager, sample_position):
    """Should mark position as settled with P&L."""
    await position_manager.save_position(sample_position)

    await position_manager.settle_position(
        position_id="pos_123",
        actual_pnl=0.05,
        winning_side="poly",
    )

    positions = await position_manager.get_open_positions()
    assert len(positions) == 0

    pos = await position_manager.get_position("pos_123")
    assert pos.status == "settled"
    assert pos.actual_pnl == 0.05


@pytest.mark.asyncio
async def test_get_daily_stats(position_manager, sample_position):
    """Should calculate daily statistics."""
    await position_manager.save_position(sample_position)
    await position_manager.settle_position("pos_123", 0.05, "poly")

    stats = await position_manager.get_daily_stats()
    assert stats["trades"] == 1
    assert stats["pnl"] == 0.05

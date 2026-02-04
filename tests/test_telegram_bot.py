"""Tests for Telegram notifier."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, UTC

from src.executor.telegram_bot import TelegramNotifier
from src.executor.models import ExecutionResult, ExecutionStatus, LegResult, OpenPosition


@pytest.fixture
def notifier():
    """Create notifier with test credentials."""
    return TelegramNotifier(bot_token="test_token", chat_id="123456")


def test_format_execution_success(notifier):
    """Should format successful execution message."""
    poly = LegResult("polymarket", True, "p1", 2.0, 0.51, 1.02)
    kalshi = LegResult("kalshi", True, "k1", 2.0, 0.49, 0.98)
    result = ExecutionResult(
        poly_leg=poly,
        kalshi_leg=kalshi,
        total_invested=2.00,
        guaranteed_payout=2.00,
        expected_profit=0.04,
    )

    msg = notifier._format_execution_message(
        result,
        event_title="Lakers vs Celtics",
        roi=2.3,
        profit=0.04,
    )

    assert "✅" in msg
    assert "Lakers vs Celtics" in msg
    assert "2.3%" in msg or "ROI" in msg


def test_format_execution_partial(notifier):
    """Should format partial fill warning."""
    poly = LegResult("polymarket", True, "p1", 2.0, 0.51, 1.02)
    kalshi = LegResult("kalshi", False, None, 0, 0, 0, "Insufficient liquidity")
    result = ExecutionResult(poly_leg=poly, kalshi_leg=kalshi)

    msg = notifier._format_execution_message(
        result,
        event_title="Lakers vs Celtics",
        roi=2.3,
        profit=0.04,
    )

    assert "⚠️" in msg or "PARTIAL" in msg
    assert "Insufficient liquidity" in msg


def test_format_execution_rolled_back(notifier):
    """Should format rolled back execution."""
    poly = LegResult("polymarket", True, "p1", 2.0, 0.51, 1.02)
    kalshi = LegResult("kalshi", False, None, 0, 0, 0, "FOK rejected")
    rollback = LegResult("polymarket_rollback", True, "rb1", 2.0, 0.49, 0.98)
    result = ExecutionResult(
        poly_leg=poly,
        kalshi_leg=kalshi,
        rollback_leg=rollback,
        rollback_loss=0.04,
    )

    msg = notifier._format_execution_message(
        result,
        event_title="Lakers vs Celtics",
        roi=2.3,
        profit=0.04,
    )

    assert "ROLLED BACK" in msg or "🔄" in msg
    assert "$0.04" in msg  # rollback loss


def test_format_execution_failed(notifier):
    """Should format failed execution."""
    poly = LegResult("polymarket", False, None, 0, 0, 0, "Error 1")
    kalshi = LegResult("kalshi", False, None, 0, 0, 0, "Error 2")
    result = ExecutionResult(poly_leg=poly, kalshi_leg=kalshi)

    msg = notifier._format_execution_message(
        result,
        event_title="Lakers vs Celtics",
        roi=2.3,
        profit=0.04,
    )

    assert "❌" in msg
    assert "FAILED" in msg


def test_format_daily_summary(notifier):
    """Should format daily summary."""
    msg = notifier._format_daily_summary(
        trades=15,
        successful=14,
        partial=1,
        pnl=0.87,
        poly_balance=10.87,
        kalshi_balance=10.43,
    )

    assert "📊" in msg
    assert "15" in msg
    assert "93%" in msg  # 14/15
    assert "$+0.87" in msg


@pytest.mark.asyncio
async def test_send_message(notifier):
    """Should send message via Telegram API."""
    with patch("src.executor.telegram_bot.Bot") as MockBot:
        mock_bot = AsyncMock()
        MockBot.return_value = mock_bot

        await notifier.send("Test message")

        mock_bot.send_message.assert_called_once()

"""Tests for risk manager."""

import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, UTC

from src.executor.risk_manager import RiskManager
from src.models import ArbitrageOpportunity, Platform


@pytest.fixture
def risk_manager():
    """Create risk manager with test settings."""
    return RiskManager(
        min_bet=1.0,
        max_bet=2.0,
        min_roi=1.0,
        max_roi=50.0,
        max_daily_trades=50,
        max_daily_loss=5.0,
        min_platform_balance=1.0,
    )


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
            "executable": True,
            "confidence": "high",
            "spread_pct": 5.0,
            "poly_token_id": "test_token_123",
            "kalshi_ticker": "KXTEST-123",
            "arb_type": "yes_no",
        },
    )


def test_kill_switch_blocks_trading(risk_manager, sample_opportunity):
    """Kill switch should block all trades."""
    risk_manager.enabled = False
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=10, kalshi_balance=10)
    assert not result.passed
    assert "kill switch" in result.reason.lower()


def test_insufficient_balance_poly(risk_manager, sample_opportunity):
    """Should reject if Polymarket balance too low."""
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=0.5, kalshi_balance=10)
    assert not result.passed
    assert "polymarket balance" in result.reason.lower()


def test_insufficient_balance_kalshi(risk_manager, sample_opportunity):
    """Should reject if Kalshi balance too low."""
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=10, kalshi_balance=0.5)
    assert not result.passed
    assert "kalshi balance" in result.reason.lower()


def test_roi_too_low(risk_manager, sample_opportunity):
    """Should reject if ROI below minimum."""
    sample_opportunity.roi_after_fees = 0.5
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=10, kalshi_balance=10)
    assert not result.passed
    assert "roi" in result.reason.lower()


def test_roi_too_high_suspicious(risk_manager, sample_opportunity):
    """Should reject suspiciously high ROI."""
    sample_opportunity.roi_after_fees = 75.0
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=10, kalshi_balance=10)
    assert not result.passed
    assert "suspicious" in result.reason.lower()


def test_daily_trade_limit(risk_manager, sample_opportunity):
    """Should reject when daily trade limit reached."""
    risk_manager._daily_trades = 50
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=10, kalshi_balance=10)
    assert not result.passed
    assert "daily" in result.reason.lower()


def test_daily_loss_limit(risk_manager, sample_opportunity):
    """Should reject when daily loss limit reached."""
    risk_manager._daily_pnl = -5.5
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=10, kalshi_balance=10)
    assert not result.passed
    assert "loss" in result.reason.lower()


def test_valid_opportunity_passes(risk_manager, sample_opportunity):
    """Valid opportunity should pass all checks."""
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=10, kalshi_balance=10)
    assert result.passed
    assert result.reason is None


def test_calculate_bet_size(risk_manager, sample_opportunity):
    """Should calculate appropriate bet size."""
    bet = risk_manager.calculate_bet_size(sample_opportunity, poly_balance=10, kalshi_balance=10)
    assert risk_manager.min_bet <= bet <= risk_manager.max_bet


def test_calculate_bet_size_respects_balance(risk_manager, sample_opportunity):
    """Bet size should not exceed available balance."""
    bet = risk_manager.calculate_bet_size(sample_opportunity, poly_balance=1.5, kalshi_balance=10)
    assert bet <= 1.5

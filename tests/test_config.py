"""Tests for executor configuration."""

from src.config import Settings


def test_executor_settings_exist():
    """Verify executor settings are defined with defaults."""
    s = Settings()

    # Polymarket trading
    assert hasattr(s, "poly_private_key")
    assert hasattr(s, "poly_funder_address")

    # Telegram
    assert hasattr(s, "telegram_bot_token")
    assert hasattr(s, "telegram_chat_id")

    # Executor limits
    assert hasattr(s, "executor_enabled")  # Disabled by default for safety
    assert s.executor_min_bet == 5.0  # Minimum $5 for better liquidity
    assert s.executor_max_bet == 10.0
    assert s.executor_min_roi == 1.0
    assert s.executor_max_daily_trades == 50
    assert s.executor_max_daily_loss == 5.0
    assert s.executor_min_platform_balance == 1.0

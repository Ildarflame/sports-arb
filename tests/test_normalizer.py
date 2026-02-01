from src.engine.normalizer import effective_buy_price, effective_sell_price, normalize_price
from src.models import MarketPrice, Platform


def test_normalize_price_clamps():
    """Pydantic enforces 0-1 range at creation, normalizer is a safety net for edge cases."""
    import pytest
    with pytest.raises(Exception):
        MarketPrice(yes_price=1.5, no_price=-0.3)


def test_normalize_price_boundary():
    p = MarketPrice(yes_price=1.0, no_price=0.0)
    result = normalize_price(p, Platform.POLYMARKET)
    assert result.yes_price == 1.0
    assert result.no_price == 0.0


def test_normalize_price_passthrough():
    p = MarketPrice(yes_price=0.65, no_price=0.35)
    result = normalize_price(p, Platform.KALSHI)
    assert result.yes_price == 0.65
    assert result.no_price == 0.35


def test_effective_buy_price_polymarket():
    # 2% fee
    price = effective_buy_price(0.50, Platform.POLYMARKET)
    assert abs(price - 0.51) < 0.001


def test_effective_buy_price_kalshi():
    # 1.5% fee
    price = effective_buy_price(0.50, Platform.KALSHI)
    assert abs(price - 0.5075) < 0.001


def test_effective_sell_price():
    price = effective_sell_price(0.50, Platform.POLYMARKET)
    assert abs(price - 0.49) < 0.001

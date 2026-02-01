from __future__ import annotations

from src.models import MarketPrice, Platform


# Fee rates per platform
FEES = {
    Platform.POLYMARKET: 0.02,   # ~2%
    Platform.KALSHI: 0.015,      # ~1.5% average
}


def normalize_price(price: MarketPrice, platform: Platform) -> MarketPrice:
    """Ensure price is in 0-1 probability format. Prices from both platforms
    should already be normalized to 0-1 by the connectors, but this acts
    as a safety net."""
    yes = max(0.0, min(1.0, price.yes_price))
    no = max(0.0, min(1.0, price.no_price))

    return MarketPrice(
        yes_price=round(yes, 4),
        no_price=round(no, 4),
        yes_bid=price.yes_bid,
        yes_ask=price.yes_ask,
        no_bid=price.no_bid,
        no_ask=price.no_ask,
        volume=price.volume,
        last_updated=price.last_updated,
    )


def effective_buy_price(price: float, platform: Platform) -> float:
    """Price including platform fee (what you actually pay)."""
    fee = FEES.get(platform, 0.02)
    return price + (price * fee)


def effective_sell_price(price: float, platform: Platform) -> float:
    """Price after platform fee (what you actually receive)."""
    fee = FEES.get(platform, 0.02)
    return price - (price * fee)

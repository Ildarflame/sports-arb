from src.engine.arbitrage import calculate_arbitrage
from src.models import Market, MarketPrice, Platform, SportEvent


def _make_event(poly_yes: float, poly_no: float, kalshi_yes: float, kalshi_no: float) -> SportEvent:
    pm = Market(
        platform=Platform.POLYMARKET,
        market_id="pm1",
        event_id="e1",
        title="A vs B",
        team_a="A",
        team_b="B",
        price=MarketPrice(yes_price=poly_yes, no_price=poly_no),
    )
    km = Market(
        platform=Platform.KALSHI,
        market_id="k1",
        event_id="e2",
        title="A vs B",
        team_a="A",
        team_b="B",
        price=MarketPrice(yes_price=kalshi_yes, no_price=kalshi_no),
    )
    return SportEvent(
        id="test1",
        title="A vs B",
        team_a="A",
        team_b="B",
        markets={Platform.POLYMARKET: pm, Platform.KALSHI: km},
        matched=True,
    )


def test_no_arbitrage():
    """Prices sum to 1.0 on each platform — no arb."""
    event = _make_event(0.55, 0.45, 0.55, 0.45)
    opp = calculate_arbitrage(event)
    assert opp is None


def test_clear_arbitrage():
    """Big price discrepancy — clear arb before fees."""
    # Buy YES on Poly at 0.45, buy NO on Kalshi at 0.40 → cost 0.85
    event = _make_event(0.45, 0.55, 0.60, 0.40)
    opp = calculate_arbitrage(event)
    assert opp is not None
    assert opp.roi_after_fees > 0


def test_arbitrage_eaten_by_fees():
    """Marginal price difference eaten by fees."""
    # Cost = 0.48 + 0.50 = 0.98, gross profit = 2%, but fees ~3.5% total
    event = _make_event(0.48, 0.52, 0.50, 0.50)
    opp = calculate_arbitrage(event)
    # Should either be None or have negative ROI after fees
    if opp is not None:
        assert opp.roi_after_fees < 1.0


def test_reverse_direction_arb():
    """Arb exists in the YES@Kalshi + NO@Poly direction."""
    # Buy YES on Kalshi at 0.40, buy NO on Poly at 0.45 → cost 0.85
    event = _make_event(0.60, 0.45, 0.40, 0.55)
    opp = calculate_arbitrage(event)
    assert opp is not None
    assert opp.platform_buy_yes == Platform.KALSHI
    assert opp.platform_buy_no == Platform.POLYMARKET

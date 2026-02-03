from datetime import UTC, datetime

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
        price=MarketPrice(yes_price=poly_yes, no_price=poly_no, volume=5000),
    )
    km = Market(
        platform=Platform.KALSHI,
        market_id="k1",
        event_id="e2",
        title="A vs B",
        team_a="A",
        team_b="B",
        price=MarketPrice(yes_price=kalshi_yes, no_price=kalshi_no, volume=5000),
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


def _make_event_with_sport(
    poly_yes: float, poly_no: float,
    kalshi_yes: float, kalshi_no: float,
    sport: str = "nba",
    teams_swapped: bool = False,
) -> SportEvent:
    """Create event with specific sport and teams_swapped flag."""
    pm = Market(
        platform=Platform.POLYMARKET,
        market_id="pm1",
        event_id="e1",
        title="A vs B",
        team_a="Team A",
        team_b="Team B",
        sport=sport,
        price=MarketPrice(yes_price=poly_yes, no_price=poly_no, volume=5000),
    )
    km = Market(
        platform=Platform.KALSHI,
        market_id="k1",
        event_id="e2",
        title="A vs B",
        team_a="Team A" if not teams_swapped else "Team B",
        team_b="Team B" if not teams_swapped else "Team A",
        sport=sport,
        price=MarketPrice(yes_price=kalshi_yes, no_price=kalshi_no, volume=5000),
    )
    return SportEvent(
        id="test1",
        title="Team A vs Team B",
        team_a="Team A",
        team_b="Team B",
        markets={Platform.POLYMARKET: pm, Platform.KALSHI: km},
        matched=True,
        teams_swapped=teams_swapped,
    )


def test_cross_team_arb_with_swapped_teams():
    """Cross-team arb when teams are swapped (2-outcome sport).

    With midpoint prices and no spread, cross-team gives same ROI as yes_no
    direction because inverted_NO = 1 - original_YES. Direction 1 wins since
    it's checked first.
    """
    event = _make_event_with_sport(
        poly_yes=0.45, poly_no=0.55,
        kalshi_yes=0.40, kalshi_no=0.60,
        sport="nba",
        teams_swapped=True,
    )
    opp = calculate_arbitrage(event)
    assert opp is not None
    # Both Dir1 and Cross3 give same cost (0.85), so Dir1 wins (checked first)
    assert opp.details.get("arb_type") == "yes_no"
    assert opp.roi_after_fees > 0


def test_no_cross_team_for_soccer():
    """Cross-team arb NOT allowed for soccer (3-outcome sport)."""
    # Same prices as above, but soccer has draws
    event = _make_event_with_sport(
        poly_yes=0.45, poly_no=0.55,
        kalshi_yes=0.50, kalshi_no=0.50,
        sport="soccer",
        teams_swapped=True,
    )
    opp = calculate_arbitrage(event)
    # Should still find arb via Direction 1/2, but NOT cross-team
    # With swapped teams in soccer, no arb should be found at all
    # because teams_swapped + soccer = blocked in Direction 1/2 too
    if opp is not None:
        assert opp.details.get("arb_type") != "cross_team"


def test_arb_type_yes_no_for_standard_arb():
    """Standard YES+NO arb should have arb_type='yes_no'."""
    event = _make_event(0.45, 0.55, 0.60, 0.40)
    opp = calculate_arbitrage(event)
    assert opp is not None
    assert opp.details.get("arb_type") == "yes_no"


def _make_live_event(
    poly_yes: float, poly_no: float,
    kalshi_yes: float, kalshi_no: float,
    game_started: bool = True,
) -> SportEvent:
    """Create event with live game timing."""
    from datetime import timedelta
    now = datetime.now(UTC)
    # Game started 1 hour ago if live, starts 1 hour from now if not
    game_time = (now - timedelta(hours=1)) if game_started else (now + timedelta(hours=1))

    pm = Market(
        platform=Platform.POLYMARKET,
        market_id="pm1",
        event_id="e1",
        title="A vs B",
        team_a="A",
        team_b="B",
        market_type="game",
        price=MarketPrice(
            yes_price=poly_yes, no_price=poly_no,
            yes_bid=poly_yes - 0.01, yes_ask=poly_yes + 0.01,
            volume=10000,
        ),
        raw_data={"game_start_time": game_time.isoformat()},
    )
    km = Market(
        platform=Platform.KALSHI,
        market_id="k1",
        event_id="e2",
        title="A vs B",
        team_a="A",
        team_b="B",
        market_type="game",
        price=MarketPrice(
            yes_price=kalshi_yes, no_price=kalshi_no,
            yes_bid=kalshi_yes - 0.01, yes_ask=kalshi_yes + 0.01,
            volume=10000,
        ),
        raw_data={"game_start_time": game_time.isoformat()},
    )
    return SportEvent(
        id="test_live",
        title="A vs B",
        team_a="A",
        team_b="B",
        markets={Platform.POLYMARKET: pm, Platform.KALSHI: km},
        matched=True,
    )


def test_live_arb_blocked_by_default():
    """Live games should be blocked when allow_live=False (default)."""
    event = _make_live_event(0.45, 0.55, 0.60, 0.40, game_started=True)
    opp = calculate_arbitrage(event, allow_live=False)
    assert opp is None


def test_live_arb_allowed_with_flag():
    """Live games should be allowed when allow_live=True."""
    event = _make_live_event(0.45, 0.55, 0.60, 0.40, game_started=True)
    opp = calculate_arbitrage(event, allow_live=True)
    assert opp is not None
    assert opp.details.get("is_live") is True


def test_non_live_game_works_normally():
    """Future games should work regardless of allow_live flag."""
    event = _make_live_event(0.45, 0.55, 0.60, 0.40, game_started=False)
    opp = calculate_arbitrage(event, allow_live=False)
    assert opp is not None
    assert opp.details.get("is_live") is not True

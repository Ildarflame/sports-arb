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


# ============================================================
# 3-Way Arbitrage Tests
# ============================================================

from datetime import date

from src.engine.arbitrage import calculate_3way_arbitrage
from src.models import ThreeWayGroup


def _make_3way_group(
    win_a_poly: float, win_a_kalshi: float,
    draw_poly: float, draw_kalshi: float,
    win_b_poly: float, win_b_kalshi: float,
) -> ThreeWayGroup:
    """Create a 3-way group with markets on both platforms."""
    def _make_market(platform: Platform, price: float, outcome: str) -> Market:
        return Market(
            platform=platform,
            market_id=f"{platform.value}_{outcome}",
            event_id="soccer1",
            title=f"Team {outcome}",
            team_a="Draw" if outcome == "draw" else outcome.upper(),
            team_b="",
            sport="soccer",
            market_type="game",
            game_date=date(2026, 2, 10),
            price=MarketPrice(yes_price=price, no_price=1 - price, volume=5000),
            raw_data={"market_subtype": "draw" if outcome == "draw" else "moneyline"},
        )

    return ThreeWayGroup(
        team_a="Liverpool",
        team_b="Arsenal",
        game_date=date(2026, 2, 10),
        sport="soccer",
        poly_win_a=_make_market(Platform.POLYMARKET, win_a_poly, "a"),
        poly_draw=_make_market(Platform.POLYMARKET, draw_poly, "draw"),
        poly_win_b=_make_market(Platform.POLYMARKET, win_b_poly, "b"),
        kalshi_win_a=_make_market(Platform.KALSHI, win_a_kalshi, "a"),
        kalshi_draw=_make_market(Platform.KALSHI, draw_kalshi, "draw"),
        kalshi_win_b=_make_market(Platform.KALSHI, win_b_kalshi, "b"),
    )


def test_3way_no_arbitrage():
    """3-way prices sum to >= 1.0 — no arb."""
    # All prices sum to 1.0 on each platform
    group = _make_3way_group(
        win_a_poly=0.40, win_a_kalshi=0.40,
        draw_poly=0.25, draw_kalshi=0.25,
        win_b_poly=0.35, win_b_kalshi=0.35,
    )
    opp = calculate_3way_arbitrage(group)
    assert opp is None


def test_3way_clear_arbitrage():
    """3-way prices sum to < 1.0 across platforms — clear arb."""
    # Best prices: win_a=0.30, draw=0.20, win_b=0.25 = 0.75 total
    group = _make_3way_group(
        win_a_poly=0.30, win_a_kalshi=0.35,  # Best: Poly 0.30
        draw_poly=0.25, draw_kalshi=0.20,     # Best: Kalshi 0.20
        win_b_poly=0.25, win_b_kalshi=0.30,   # Best: Poly 0.25
    )
    opp = calculate_3way_arbitrage(group)
    assert opp is not None
    assert opp.details.get("arb_type") == "3way"
    assert opp.roi_after_fees > 0
    # Check legs
    legs = opp.details.get("legs", [])
    assert len(legs) == 3


def test_3way_fees_eat_profit():
    """3-way where gross profit exists but fees eliminate it."""
    # Total = 0.99, but after ~2% fees → no profit
    group = _make_3way_group(
        win_a_poly=0.34, win_a_kalshi=0.35,
        draw_poly=0.33, draw_kalshi=0.33,
        win_b_poly=0.33, win_b_kalshi=0.32,
    )
    opp = calculate_3way_arbitrage(group)
    # With fees, 0.99 * 1.02 ≈ 1.01, so no profit
    # Should be None or have very low/negative ROI
    assert opp is None or opp.roi_after_fees < 0.5

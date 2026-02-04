from __future__ import annotations

import logging
from datetime import UTC, datetime

from src.config import settings
from src.engine.normalizer import FEES, normalize_price
from src.models import ArbitrageOpportunity, Market, MarketPrice, Platform, SportEvent, ThreeWayGroup

logger = logging.getLogger(__name__)

# Sports where draws are possible — cross-team YES+YES is invalid
# (one of the two YESes must win only in 2-outcome sports)
_THREE_OUTCOME_SPORTS = {"soccer", "rugby", "cricket"}


def _invert_price(p: MarketPrice) -> MarketPrice:
    """Swap YES and NO sides of a price (for teams_swapped events)."""
    return MarketPrice(
        yes_price=p.no_price,
        no_price=p.yes_price,
        yes_bid=round(1 - p.yes_ask, 4) if p.yes_ask is not None else p.no_bid,
        yes_ask=round(1 - p.yes_bid, 4) if p.yes_bid is not None else p.no_ask,
        no_bid=round(1 - p.no_ask, 4) if p.no_ask is not None else p.yes_bid,
        no_ask=round(1 - p.no_bid, 4) if p.no_bid is not None else p.yes_ask,
        volume=p.volume,
        last_updated=p.last_updated,
    )


def _get_poly_token(raw_data: dict, index: int) -> str | None:
    """Safely get Polymarket token ID by index (0=team_a, 1=team_b)."""
    tokens = raw_data.get("clob_token_ids", [])
    return tokens[index] if len(tokens) > index else None


def _parse_iso_datetime(dt_str: str | None) -> datetime | None:
    """Parse ISO datetime string to datetime object."""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _is_market_expired(
    market_raw_data: dict, market_type: str, allow_live: bool = False
) -> tuple[bool, bool]:
    """Check if market event has already started (game) or expired (futures).

    For game markets: skip if game has already started (stale prices), unless allow_live=True
    For all markets: skip if close/end time has passed

    Returns:
        (is_expired, is_live): is_expired=True means skip this market,
                              is_live=True means game is in progress
    """
    now = datetime.now(UTC)

    # Polymarket fields
    game_start = _parse_iso_datetime(market_raw_data.get("game_start_time"))
    end_date = _parse_iso_datetime(market_raw_data.get("end_date"))

    # Kalshi fields
    close_time = _parse_iso_datetime(market_raw_data.get("close_time"))
    expiration_time = _parse_iso_datetime(market_raw_data.get("expiration_time"))

    # Check if market close/end time has passed — always expired
    market_end = end_date or close_time or expiration_time
    if market_end and market_end < now:
        return True, False

    # For game markets: check if game has started
    is_live = False
    if market_type == "game" and game_start and game_start < now:
        is_live = True
        # If live not allowed, mark as expired
        if not allow_live:
            return True, True

    return False, is_live


def _exec_buy_price(price: MarketPrice, side: str) -> float:
    """Executable price for buying YES or NO side.

    Buy YES → pay yes_ask (worst case for buyer).
    Buy NO  → pay 1 - yes_bid = no_ask (worst case for buyer).
    Falls back to midpoint if bid/ask not available.
    """
    if side == "yes":
        if price.yes_ask and price.yes_ask > 0:
            return price.yes_ask
        return price.yes_price
    else:
        # Buy NO = pay 1 - yes_bid (= no_ask)
        if price.yes_bid and price.yes_bid > 0:
            return round(1.0 - price.yes_bid, 4)
        if price.no_ask and price.no_ask > 0:
            return price.no_ask
        return price.no_price


def calculate_arbitrage(
    event: SportEvent, allow_live: bool | None = None
) -> ArbitrageOpportunity | None:
    """Check for arbitrage opportunity on a matched event.

    Strategy: Buy YES on one platform and NO on another.
    If total cost < 1.0, there's profit (one side always pays out 1.0).

    We check both directions:
      1) YES on Polymarket + NO on Kalshi
      2) YES on Kalshi + NO on Polymarket

    Args:
        event: The matched sport event to check
        allow_live: Override for live mode. If None, uses settings.allow_live_arbs
    """
    poly_market = event.markets.get(Platform.POLYMARKET)
    kalshi_market = event.markets.get(Platform.KALSHI)

    if not poly_market or not kalshi_market:
        return None
    if not poly_market.price or not kalshi_market.price:
        return None

    # Determine if live mode is enabled
    live_enabled = allow_live if allow_live is not None else settings.allow_live_arbs

    # F0.1: Skip markets where event has already started or market expired
    market_type = poly_market.market_type or kalshi_market.market_type or "game"
    poly_expired, poly_is_live = _is_market_expired(
        poly_market.raw_data, market_type, allow_live=live_enabled
    )
    if poly_expired:
        logger.debug(f"Skipping {event.title}: Polymarket event expired/started")
        return None
    kalshi_expired, kalshi_is_live = _is_market_expired(
        kalshi_market.raw_data, market_type, allow_live=live_enabled
    )
    if kalshi_expired:
        logger.debug(f"Skipping {event.title}: Kalshi event expired/started")
        return None

    # Track if this is a live game
    is_live = poly_is_live or kalshi_is_live

    pp = normalize_price(poly_market.price, Platform.POLYMARKET)
    kp = normalize_price(kalshi_market.price, Platform.KALSHI)


    # If teams are swapped between platforms, invert Kalshi YES/NO
    # so that YES on both platforms refers to the same team winning
    if event.teams_swapped:
        kp = _invert_price(kp)

    poly_url = poly_market.url or ""
    kalshi_url = kalshi_market.url or ""
    market_subtype = poly_market.raw_data.get("market_subtype", "moneyline")
    # Get line for spread/O-U markets
    market_line = poly_market.line or kalshi_market.line
    # Get map number for esports
    map_number = poly_market.map_number or kalshi_market.map_number

    # Skip markets with insufficient liquidity — need volume on BOTH platforms
    poly_vol = poly_market.price.volume if poly_market.price else 0
    kalshi_vol = kalshi_market.price.volume if kalshi_market.price else 0
    if (poly_vol or 0) == 0 or (kalshi_vol or 0) == 0:
        return None
    combined_vol = (poly_vol or 0) + (kalshi_vol or 0)
    # Q3: Minimum volume threshold (lowered for more candidates)
    if combined_vol < 100:
        return None

    # Compute bid-ask spread percentage for liquidity check
    spread_pct = None
    if pp.yes_bid is not None and pp.yes_ask is not None and pp.yes_ask > 0:
        spread_pct = ((pp.yes_ask - pp.yes_bid) / pp.yes_ask) * 100

    # Q2: Skip markets with wide bid-ask spread (relaxed for more candidates)
    if spread_pct is not None and spread_pct > 50:
        logger.debug(f"Skipping {event.title}: spread too wide ({spread_pct:.0f}%)")
        return None

    # Skip markets with no liquidity (price at 0 or 1)
    MIN_PRICE = 0.01
    MAX_PRICE = 0.99
    if not (MIN_PRICE <= pp.yes_price <= MAX_PRICE and MIN_PRICE <= pp.no_price <= MAX_PRICE):
        return None
    if not (MIN_PRICE <= kp.yes_price <= MAX_PRICE and MIN_PRICE <= kp.no_price <= MAX_PRICE):
        return None

    best_opp: ArbitrageOpportunity | None = None
    best_roi = 0.0

    # Executable prices (bid/ask) for accurate cost calculation
    poly_yes_exec = _exec_buy_price(pp, "yes")
    poly_no_exec = _exec_buy_price(pp, "no")
    kalshi_yes_exec = _exec_buy_price(kp, "yes")
    kalshi_no_exec = _exec_buy_price(kp, "no")

    # Determine if we have real bid/ask data (not just midpoint)
    has_exec_d1 = bool(pp.yes_ask and kp.yes_bid)
    has_exec_d2 = bool(kp.yes_ask and pp.yes_bid)

    # Direction 1: Buy YES on Polymarket, buy NO on Kalshi
    # After inversion (if teams_swapped), kp.yes = same team as pp.yes
    # So buying kp.no = buying the OTHER team = kalshi's original YES team
    midpoint_cost_1 = pp.yes_price + kp.no_price
    exec_cost_1 = poly_yes_exec + kalshi_no_exec
    cost_1 = exec_cost_1

    # Determine actual teams and sides for clear display
    # poly YES = poly_team_a, poly NO = poly_team_b
    # After inversion: kalshi YES = poly_team_a, kalshi NO = poly_team_b (conceptually)
    # But on actual Kalshi market: kalshi_team_a is always YES
    d1_poly_team = poly_market.team_a  # buying Poly YES = team_a
    d1_poly_side = "YES"
    if event.teams_swapped:
        # Inverted NO = original YES = kalshi_team_a
        d1_kalshi_team = kalshi_market.team_a
        d1_kalshi_side = "YES"  # actual market side!
    else:
        # Not swapped: NO = kalshi_team_b
        d1_kalshi_team = kalshi_market.team_b
        d1_kalshi_side = "NO"

    if cost_1 < 1.0:
        gross_profit = 1.0 - cost_1
        # Apply fees
        fee_poly = poly_yes_exec * FEES[Platform.POLYMARKET]
        fee_kalshi = kalshi_no_exec * FEES[Platform.KALSHI]
        net_profit = gross_profit - fee_poly - fee_kalshi
        net_cost = cost_1 + fee_poly + fee_kalshi
        roi = (net_profit / net_cost) * 100 if net_cost > 0 else 0

        if roi > best_roi:
            best_roi = roi
            best_opp = ArbitrageOpportunity(
                event_title=event.title,
                team_a=event.team_a,
                team_b=event.team_b,
                platform_buy_yes=Platform.POLYMARKET,
                platform_buy_no=Platform.KALSHI,
                yes_price=poly_yes_exec,
                no_price=kalshi_no_exec,
                total_cost=round(net_cost, 4),
                profit_pct=round(gross_profit * 100, 2),
                roi_after_fees=round(roi, 2),
                found_at=datetime.now(UTC),
                details={
                    "arb_type": "yes_no",
                    "poly_yes": pp.yes_price,
                    "poly_no": pp.no_price,
                    "kalshi_yes": kp.yes_price,
                    "kalshi_no": kp.no_price,
                    "poly_yes_bid": pp.yes_bid,
                    "poly_yes_ask": pp.yes_ask,
                    "kalshi_yes_bid": kp.yes_bid,
                    "kalshi_yes_ask": kp.yes_ask,
                    "direction": f"{d1_poly_team}@Poly + {d1_kalshi_team}@Kalshi",
                    "poly_action": f"Buy {d1_poly_side} ({d1_poly_team})",
                    "kalshi_action": f"Buy {d1_kalshi_side} ({d1_kalshi_team})",
                    "teams_swapped": event.teams_swapped,
                    "poly_team_a": poly_market.team_a,
                    "poly_team_b": poly_market.team_b,
                    "kalshi_team_a": kalshi_market.team_a,
                    "line": market_line,
                    "map_number": map_number,
                    "kalshi_team_b": kalshi_market.team_b,
                    "poly_url": poly_url,
                    "kalshi_url": kalshi_url,
                    "poly_volume": poly_vol,
                    "kalshi_volume": kalshi_vol,
                    "spread_pct": round(spread_pct, 1) if spread_pct is not None else None,
                    "midpoint_cost": round(midpoint_cost_1, 4),
                    "exec_cost": round(exec_cost_1, 4),
                    "executable": has_exec_d1,
                    "market_subtype": market_subtype,
                    "market_type": market_type,
                    # Trading identifiers for executor
                    "poly_token_id": _get_poly_token(poly_market.raw_data, 0),  # team_a token
                    "poly_side": "BUY",
                    "kalshi_ticker": kalshi_market.market_id,
                    "kalshi_side": "no" if not event.teams_swapped else "yes",
                },
            )

    # Direction 2: Buy YES on Kalshi, buy NO on Polymarket
    # kp.yes (after inversion if swapped) = poly_team_a
    # pp.no = poly_team_b
    midpoint_cost_2 = kp.yes_price + pp.no_price
    exec_cost_2 = kalshi_yes_exec + poly_no_exec
    cost_2 = exec_cost_2

    # Determine actual teams and sides for clear display
    d2_poly_team = poly_market.team_b  # buying Poly NO = team_b
    d2_poly_side = "NO"
    if event.teams_swapped:
        # Inverted YES = original NO = kalshi_team_b (the other team)
        d2_kalshi_team = kalshi_market.team_b
        d2_kalshi_side = "NO"  # actual market side!
    else:
        # Not swapped: YES = kalshi_team_a
        d2_kalshi_team = kalshi_market.team_a
        d2_kalshi_side = "YES"

    if cost_2 < 1.0:
        gross_profit = 1.0 - cost_2
        fee_kalshi = kalshi_yes_exec * FEES[Platform.KALSHI]
        fee_poly = poly_no_exec * FEES[Platform.POLYMARKET]
        net_profit = gross_profit - fee_kalshi - fee_poly
        net_cost = cost_2 + fee_kalshi + fee_poly
        roi = (net_profit / net_cost) * 100 if net_cost > 0 else 0

        if roi > best_roi:
            best_opp = ArbitrageOpportunity(
                event_title=event.title,
                team_a=event.team_a,
                team_b=event.team_b,
                platform_buy_yes=Platform.KALSHI,
                platform_buy_no=Platform.POLYMARKET,
                yes_price=kalshi_yes_exec,
                no_price=poly_no_exec,
                total_cost=round(net_cost, 4),
                profit_pct=round(gross_profit * 100, 2),
                roi_after_fees=round(roi, 2),
                found_at=datetime.now(UTC),
                details={
                    "arb_type": "yes_no",
                    "poly_yes": pp.yes_price,
                    "poly_no": pp.no_price,
                    "kalshi_yes": kp.yes_price,
                    "kalshi_no": kp.no_price,
                    "poly_yes_bid": pp.yes_bid,
                    "poly_yes_ask": pp.yes_ask,
                    "kalshi_yes_bid": kp.yes_bid,
                    "kalshi_yes_ask": kp.yes_ask,
                    "direction": f"{d2_kalshi_team}@Kalshi + {d2_poly_team}@Poly",
                    "poly_action": f"Buy {d2_poly_side} ({d2_poly_team})",
                    "kalshi_action": f"Buy {d2_kalshi_side} ({d2_kalshi_team})",
                    "teams_swapped": event.teams_swapped,
                    "poly_team_a": poly_market.team_a,
                    "poly_team_b": poly_market.team_b,
                    "kalshi_team_a": kalshi_market.team_a,
                    "kalshi_team_b": kalshi_market.team_b,
                    "line": market_line,
                    "map_number": map_number,
                    "poly_url": poly_url,
                    "kalshi_url": kalshi_url,
                    "poly_volume": poly_vol,
                    "kalshi_volume": kalshi_vol,
                    "spread_pct": round(spread_pct, 1) if spread_pct is not None else None,
                    "midpoint_cost": round(midpoint_cost_2, 4),
                    "exec_cost": round(exec_cost_2, 4),
                    "executable": has_exec_d2,
                    "market_subtype": market_subtype,
                    "market_type": market_type,
                    # Trading identifiers for executor
                    "poly_token_id": _get_poly_token(poly_market.raw_data, 1),  # team_b token
                    "poly_side": "BUY",
                    "kalshi_ticker": kalshi_market.market_id,
                    "kalshi_side": "yes" if not event.teams_swapped else "no",
                },
            )

    # Direction 3-4: Cross-team arbitrage (YES_A + YES_B)
    # Only valid for 2-outcome sports where exactly one team wins
    sport = poly_market.sport or kalshi_market.sport or ""
    if sport not in _THREE_OUTCOME_SPORTS and not event.teams_swapped:
        # For cross-team, we need ORIGINAL Kalshi prices (before any inversion)
        # Since teams_swapped=False here, kp already has original prices
        # Cross-team: Buy Poly YES (team_a wins) + Kalshi YES_B (team_b wins)
        # This works because: if team_a wins → Poly YES pays, if team_b wins → Kalshi YES pays
        # We need Kalshi's team_b YES price, but Kalshi markets are per-team
        # So we use: Poly YES_A + (1 - Kalshi YES_A) conceptually equals team_a + team_b coverage
        # Actually for cross-team: we buy Poly team_a YES + Kalshi original NO (which is team_b)
        # This is Direction 1 when not swapped. Let me reconsider...
        #
        # Cross-team scenario: Poly has "Team A to win" (YES=A wins, NO=A loses)
        # Kalshi has separate markets: "Team A to win" and "Team B to win"
        # We want: Poly YES_A (0.45) + Kalshi YES_B (0.50) = 0.95 < 1.0
        # But current model only matches ONE Kalshi market (the team_a market)
        # So kalshi.yes = A wins, kalshi.no = A doesn't win
        # For 2-outcome: A doesn't win = B wins, so kalshi.no = B wins
        # Thus: Poly YES_A + Kalshi NO_A = YES_A + YES_B (conceptually)
        # This IS Direction 1 already! So cross-team is already covered when teams align.
        #
        # The TRUE cross-team case is when teams_swapped=True:
        # Poly team_a = Kalshi team_b (swapped), so:
        # Poly YES = Poly team_a wins = Kalshi team_b wins
        # To get coverage: Poly YES + Kalshi YES (original, before inversion)
        # = Poly team_a + Kalshi team_a = covers both outcomes
        pass

    # For teams_swapped cases in 2-outcome sports:
    # We can do cross-team by using ORIGINAL Kalshi prices
    if sport not in _THREE_OUTCOME_SPORTS and event.teams_swapped:
        # Original Kalshi: YES = kalshi_team_a, NO = kalshi_team_b
        # After swap: Poly team_a = Kalshi team_b
        # Cross-team: Poly YES (poly_team_a) + Kalshi original YES (kalshi_team_a)
        # = poly_team_a wins + kalshi_team_a wins
        # Since poly_team_a = kalshi_team_b, this covers: kalshi_team_b wins OR kalshi_team_a wins
        # = all outcomes (for 2-outcome sport)
        kp_original = normalize_price(kalshi_market.price, Platform.KALSHI)

        # Direction 3: Poly YES + Kalshi original YES (cross-team)
        cross_cost_3 = poly_yes_exec + _exec_buy_price(kp_original, "yes")
        if cross_cost_3 < 1.0:
            gross_profit = 1.0 - cross_cost_3
            kalshi_orig_yes_exec = _exec_buy_price(kp_original, "yes")
            fee_poly = poly_yes_exec * FEES[Platform.POLYMARKET]
            fee_kalshi = kalshi_orig_yes_exec * FEES[Platform.KALSHI]
            net_profit = gross_profit - fee_poly - fee_kalshi
            net_cost = cross_cost_3 + fee_poly + fee_kalshi
            roi = (net_profit / net_cost) * 100 if net_cost > 0 else 0

            # Cross-team: poly_team_a + kalshi_team_a (they're different teams due to swap)
            d3_poly_team = poly_market.team_a
            d3_kalshi_team = kalshi_market.team_a

            if roi > best_roi:
                best_roi = roi
                best_opp = ArbitrageOpportunity(
                    event_title=event.title,
                    team_a=event.team_a,
                    team_b=event.team_b,
                    platform_buy_yes=Platform.POLYMARKET,
                    platform_buy_no=Platform.KALSHI,  # conceptually buying "other team YES"
                    yes_price=poly_yes_exec,
                    no_price=kalshi_orig_yes_exec,
                    total_cost=round(net_cost, 4),
                    profit_pct=round(gross_profit * 100, 2),
                    roi_after_fees=round(roi, 2),
                    found_at=datetime.now(UTC),
                    details={
                        "arb_type": "cross_team",
                        "poly_yes": pp.yes_price,
                        "poly_no": pp.no_price,
                        "kalshi_yes": kp_original.yes_price,
                        "kalshi_no": kp_original.no_price,
                        "poly_yes_bid": pp.yes_bid,
                        "poly_yes_ask": pp.yes_ask,
                        "kalshi_yes_bid": kp_original.yes_bid,
                        "kalshi_yes_ask": kp_original.yes_ask,
                        "direction": f"{d3_poly_team}@Poly + {d3_kalshi_team}@Kalshi (CROSS)",
                        "poly_action": f"Buy YES ({d3_poly_team})",
                        "kalshi_action": f"Buy YES ({d3_kalshi_team})",
                        "teams_swapped": event.teams_swapped,
                        "poly_team_a": poly_market.team_a,
                        "poly_team_b": poly_market.team_b,
                        "kalshi_team_a": kalshi_market.team_a,
                        "kalshi_team_b": kalshi_market.team_b,
                        "line": market_line,
                    "map_number": map_number,
                        "poly_url": poly_url,
                        "kalshi_url": kalshi_url,
                        "poly_volume": poly_vol,
                        "kalshi_volume": kalshi_vol,
                        "spread_pct": round(spread_pct, 1) if spread_pct is not None else None,
                        "midpoint_cost": round(cross_cost_3, 4),
                        "exec_cost": round(cross_cost_3, 4),
                        "executable": bool(pp.yes_ask and kp_original.yes_ask),
                        "market_subtype": market_subtype,
                        "market_type": market_type,
                        # Trading identifiers for executor
                        "poly_token_id": _get_poly_token(poly_market.raw_data, 0),  # team_a token
                        "poly_side": "BUY",
                        "kalshi_ticker": kalshi_market.market_id,
                        "kalshi_side": "yes",
                    },
                )

        # Direction 4: Poly NO + Kalshi original NO (cross-team, opposite direction)
        # Poly NO = poly_team_b wins, Kalshi original NO = kalshi_team_b wins
        # Since poly_team_a = kalshi_team_b (swapped), poly_team_b = kalshi_team_a
        # So: poly_team_b + kalshi_team_b = kalshi_team_a + kalshi_team_b = all outcomes
        cross_cost_4 = poly_no_exec + _exec_buy_price(kp_original, "no")
        if cross_cost_4 < 1.0:
            gross_profit = 1.0 - cross_cost_4
            kalshi_orig_no_exec = _exec_buy_price(kp_original, "no")
            fee_poly = poly_no_exec * FEES[Platform.POLYMARKET]
            fee_kalshi = kalshi_orig_no_exec * FEES[Platform.KALSHI]
            net_profit = gross_profit - fee_poly - fee_kalshi
            net_cost = cross_cost_4 + fee_poly + fee_kalshi
            roi = (net_profit / net_cost) * 100 if net_cost > 0 else 0

            # Cross-team: poly_team_b + kalshi_team_b
            d4_poly_team = poly_market.team_b
            d4_kalshi_team = kalshi_market.team_b

            if roi > best_roi:
                best_roi = roi
                best_opp = ArbitrageOpportunity(
                    event_title=event.title,
                    team_a=event.team_a,
                    team_b=event.team_b,
                    platform_buy_yes=Platform.POLYMARKET,
                    platform_buy_no=Platform.KALSHI,
                    yes_price=poly_no_exec,
                    no_price=kalshi_orig_no_exec,
                    total_cost=round(net_cost, 4),
                    profit_pct=round(gross_profit * 100, 2),
                    roi_after_fees=round(roi, 2),
                    found_at=datetime.now(UTC),
                    details={
                        "arb_type": "cross_team",
                        "poly_yes": pp.yes_price,
                        "poly_no": pp.no_price,
                        "kalshi_yes": kp_original.yes_price,
                        "kalshi_no": kp_original.no_price,
                        "poly_yes_bid": pp.yes_bid,
                        "poly_yes_ask": pp.yes_ask,
                        "kalshi_yes_bid": kp_original.yes_bid,
                        "kalshi_yes_ask": kp_original.yes_ask,
                        "direction": f"{d4_poly_team}@Poly + {d4_kalshi_team}@Kalshi (CROSS)",
                        "poly_action": f"Buy NO ({d4_poly_team})",
                        "kalshi_action": f"Buy NO ({d4_kalshi_team})",
                        "teams_swapped": event.teams_swapped,
                        "poly_team_a": poly_market.team_a,
                        "poly_team_b": poly_market.team_b,
                        "kalshi_team_a": kalshi_market.team_a,
                        "kalshi_team_b": kalshi_market.team_b,
                        "line": market_line,
                    "map_number": map_number,
                        "poly_url": poly_url,
                        "kalshi_url": kalshi_url,
                        "poly_volume": poly_vol,
                        "kalshi_volume": kalshi_vol,
                        "spread_pct": round(spread_pct, 1) if spread_pct is not None else None,
                        "midpoint_cost": round(cross_cost_4, 4),
                        "exec_cost": round(cross_cost_4, 4),
                        "executable": bool(pp.yes_bid and kp_original.yes_bid),
                        "market_subtype": market_subtype,
                        "market_type": market_type,
                        # Trading identifiers for executor
                        "poly_token_id": _get_poly_token(poly_market.raw_data, 1),  # team_b token
                        "poly_side": "BUY",
                        "kalshi_ticker": kalshi_market.market_id,
                        "kalshi_side": "no",
                    },
                )

    if best_opp:
        # Flag suspicious: wide bid-ask spread suggests illiquidity
        if spread_pct is not None and spread_pct > 20:
            best_opp.details["suspicious"] = True
            best_opp.details["suspicious_reason"] = f"wide spread ({spread_pct:.0f}%)"

        # Flag suspicious ROI (likely due to stale prices or no liquidity)
        if best_opp.roi_after_fees > 100:
            best_opp.details["suspicious"] = True
            logger.warning(
                f"SUSPICIOUS ARB: {event.title} | ROI={best_opp.roi_after_fees}% "
                f"(likely stale/illiquid)"
            )
        else:
            logger.info(
                f"ARB FOUND: {event.title} | ROI={best_opp.roi_after_fees}% | "
                f"Cost={best_opp.total_cost}"
            )

        # Compute confidence: high/medium/low based on data quality
        has_poly_exec = bool(pp.yes_ask and pp.yes_bid)
        has_kalshi_exec = bool(kp.yes_ask and kp.yes_bid)
        has_both_exec = has_poly_exec and has_kalshi_exec
        combined_vol = (poly_vol or 0) + (kalshi_vol or 0)
        narrow_spread = spread_pct is not None and spread_pct < 15

        best_opp.details["has_poly_exec"] = has_poly_exec
        best_opp.details["has_kalshi_exec"] = has_kalshi_exec

        if has_both_exec and combined_vol > 5000 and narrow_spread:
            best_opp.details["confidence"] = "high"
        elif (has_poly_exec or has_kalshi_exec) or combined_vol > 1000:
            best_opp.details["confidence"] = "medium"
        else:
            best_opp.details["confidence"] = "low"

        # Q5: Liquidity analysis - how much can be executed at these prices
        try:
            from src.engine.liquidity import analyze_arbitrage_liquidity

            liquidity = analyze_arbitrage_liquidity(
                event=event,
                buy_yes_platform=best_opp.platform_buy_yes,
                yes_price=best_opp.yes_price,
                no_price=best_opp.no_price,
            )
            if liquidity:
                best_opp.details["liquidity"] = liquidity.to_dict()
        except Exception as e:
            logger.debug(f"Liquidity analysis failed for {event.title}: {e}")

        # Q4: Auto-eligible flag for automated trading
        is_suspicious = best_opp.details.get("suspicious", False)
        conf = best_opp.details.get("confidence", "low")
        best_opp.details["auto_eligible"] = (
            conf == "high"
            and not is_suspicious
            and narrow_spread
        )

        # Live game handling
        if is_live:
            best_opp.details["is_live"] = True
            # Stricter validation for live arbs
            live_valid = _validate_live_arb(best_opp)
            if not live_valid:
                logger.info(
                    f"LIVE ARB REJECTED: {event.title} (failed live validation)"
                )
                return None
            logger.info(
                f"LIVE ARB: {event.title} | ROI={best_opp.roi_after_fees}%"
            )

    return best_opp


def _validate_live_arb(opp: ArbitrageOpportunity) -> bool:
    """Stricter validation for live (in-progress) game arbitrage.

    Live games have faster price movements and higher risk of stale prices,
    so we require higher confidence levels.
    """
    # Must have real bid/ask data (not just midpoint)
    if not opp.details.get("executable"):
        return False

    # Must meet minimum confidence
    conf = opp.details.get("confidence", "low")
    min_conf = settings.live_min_confidence
    conf_levels = {"low": 0, "medium": 1, "high": 2}
    if conf_levels.get(conf, 0) < conf_levels.get(min_conf, 2):
        return False

    # Spread must be tight
    spread = opp.details.get("spread_pct")
    if spread is not None and spread > settings.live_max_spread_pct:
        return False

    # ROI must not be suspiciously high (likely stale prices)
    if opp.roi_after_fees > settings.live_max_roi:
        return False

    # Cannot be flagged as suspicious
    if opp.details.get("suspicious"):
        return False

    return True


def calculate_bet_sizes(
    yes_price: float,
    no_price: float,
    yes_platform: Platform,
    no_platform: Platform,
    bankroll: float = 100.0,
) -> dict | None:
    """Calculate optimal bet sizes for an arbitrage opportunity.

    For a binary arb (YES on one platform + NO on another), allocate
    bankroll proportionally so that guaranteed profit is maximized.

    Returns dict with yes_bet, no_bet, guaranteed_profit, roi_on_capital,
    or None if inputs are invalid (no arb exists or prices are invalid).
    """
    # Validate inputs - prices must be in valid range
    if yes_price <= 0 or yes_price >= 1 or no_price <= 0 or no_price >= 1:
        return None

    fee_yes = FEES.get(yes_platform, 0.02)
    fee_no = FEES.get(no_platform, 0.02)

    # Cost per unit including fees
    cost_yes = yes_price * (1 + fee_yes)
    cost_no = no_price * (1 + fee_no)
    total_cost_per_unit = cost_yes + cost_no

    # No arbitrage if total cost >= 1.0 (would lose money)
    if total_cost_per_unit >= 1.0:
        return None

    # Buy `units` contracts: each pays $1, costs total_cost_per_unit
    units = bankroll / total_cost_per_unit
    yes_bet = round(cost_yes * units, 2)
    no_bet = round(cost_no * units, 2)
    profit = round((1.0 - total_cost_per_unit) * units, 2)
    roi = round((1.0 - total_cost_per_unit) / total_cost_per_unit * 100, 2)

    return {
        "yes_bet": yes_bet,
        "no_bet": no_bet,
        "guaranteed_profit": profit,
        "roi_on_capital": roi,
    }


def calculate_3way_arbitrage(
    group: "ThreeWayGroup", allow_live: bool | None = None
) -> ArbitrageOpportunity | None:
    """Calculate 3-way arbitrage opportunity for soccer matches.

    For each outcome (Win A, Draw, Win B), find the cheapest YES price
    across both platforms. If total cost < 1.0, we have arbitrage.

    Args:
        group: ThreeWayGroup with markets from both platforms
        allow_live: Override for live mode. If None, uses settings.allow_live_arbs

    Returns:
        ArbitrageOpportunity if arbitrage found, None otherwise
    """
    from src.models import ThreeWayGroup

    live_enabled = allow_live if allow_live is not None else settings.allow_live_arbs

    def _get_best_price(poly_m: "Market | None", kalshi_m: "Market | None") -> tuple[float, Platform, "Market | None"]:
        """Get the best (lowest) YES price for an outcome across platforms."""
        poly_price = None
        kalshi_price = None

        if poly_m and poly_m.price:
            # Check expiration
            expired, _ = _is_market_expired(poly_m.raw_data, "game", allow_live=live_enabled)
            if not expired:
                poly_price = _exec_buy_price(
                    normalize_price(poly_m.price, Platform.POLYMARKET), "yes"
                )

        if kalshi_m and kalshi_m.price:
            expired, _ = _is_market_expired(kalshi_m.raw_data, "game", allow_live=live_enabled)
            if not expired:
                kalshi_price = _exec_buy_price(
                    normalize_price(kalshi_m.price, Platform.KALSHI), "yes"
                )

        if poly_price is None and kalshi_price is None:
            return 0, Platform.POLYMARKET, None

        if poly_price is not None and kalshi_price is not None:
            if poly_price <= kalshi_price:
                return poly_price, Platform.POLYMARKET, poly_m
            else:
                return kalshi_price, Platform.KALSHI, kalshi_m
        elif poly_price is not None:
            return poly_price, Platform.POLYMARKET, poly_m
        else:
            return kalshi_price, Platform.KALSHI, kalshi_m

    # Get best price for each outcome
    win_a_price, win_a_platform, win_a_market = _get_best_price(group.poly_win_a, group.kalshi_win_a)
    draw_price, draw_platform, draw_market = _get_best_price(group.poly_draw, group.kalshi_draw)
    win_b_price, win_b_platform, win_b_market = _get_best_price(group.poly_win_b, group.kalshi_win_b)

    # Need all 3 outcomes to have valid prices
    if win_a_price <= 0 or draw_price <= 0 or win_b_price <= 0:
        logger.debug(f"3-way {group.team_a} vs {group.team_b}: missing prices (A={win_a_price}, D={draw_price}, B={win_b_price})")
        return None

    # Calculate total cost (before fees)
    total_cost = win_a_price + draw_price + win_b_price

    # Log only real arb candidates (cost < 1.0)
    if total_cost < 1.0:
        logger.debug(f"3-way {group.team_a} vs {group.team_b}: cost={total_cost:.4f} (A={win_a_price:.3f}@{win_a_platform.value}, D={draw_price:.3f}@{draw_platform.value}, B={win_b_price:.3f}@{win_b_platform.value})")

    # Check for arbitrage
    if total_cost >= 1.0:
        return None

    # Calculate profit and fees
    gross_profit = 1.0 - total_cost

    fee_win_a = win_a_price * FEES.get(win_a_platform, 0.02)
    fee_draw = draw_price * FEES.get(draw_platform, 0.02)
    fee_win_b = win_b_price * FEES.get(win_b_platform, 0.02)
    total_fees = fee_win_a + fee_draw + fee_win_b

    net_profit = gross_profit - total_fees
    net_cost = total_cost + total_fees

    if net_profit <= 0:
        return None

    roi = (net_profit / net_cost) * 100 if net_cost > 0 else 0

    # Build legs for display
    legs = [
        {
            "outcome": f"Win {group.team_a}",
            "platform": win_a_platform.value,
            "price": round(win_a_price, 4),
            "url": win_a_market.url if win_a_market else "",
        },
        {
            "outcome": "Draw",
            "platform": draw_platform.value,
            "price": round(draw_price, 4),
            "url": draw_market.url if draw_market else "",
        },
        {
            "outcome": f"Win {group.team_b}",
            "platform": win_b_platform.value,
            "price": round(win_b_price, 4),
            "url": win_b_market.url if win_b_market else "",
        },
    ]

    # Get combined volume
    total_vol = 0
    for m in [win_a_market, draw_market, win_b_market]:
        if m and m.price:
            total_vol += m.price.volume or 0

    # Create opportunity
    opp = ArbitrageOpportunity(
        event_title=f"{group.team_a} vs {group.team_b}",
        team_a=group.team_a,
        team_b=group.team_b,
        platform_buy_yes=win_a_platform,  # Primary platform (for the first leg)
        platform_buy_no=draw_platform,  # Secondary platform (for display)
        yes_price=win_a_price,
        no_price=draw_price + win_b_price,  # Combined cost of other legs
        total_cost=round(net_cost, 4),
        profit_pct=round(gross_profit * 100, 2),
        roi_after_fees=round(roi, 2),
        found_at=datetime.now(UTC),
        details={
            "arb_type": "3way",
            "legs": legs,
            "win_a_price": round(win_a_price, 4),
            "draw_price": round(draw_price, 4),
            "win_b_price": round(win_b_price, 4),
            "win_a_platform": win_a_platform.value,
            "draw_platform": draw_platform.value,
            "win_b_platform": win_b_platform.value,
            "direction": f"{group.team_a}@{win_a_platform.value} + Draw@{draw_platform.value} + {group.team_b}@{win_b_platform.value}",
            "sport": group.sport,
            "game_date": group.game_date.isoformat() if group.game_date else None,
            "combined_volume": total_vol,
            "market_type": "game",
            "market_subtype": "3way",
        },
    )

    logger.info(
        f"3-WAY ARB FOUND: {group.team_a} vs {group.team_b} | "
        f"ROI={roi:.2f}% | Cost={net_cost:.4f} | "
        f"Legs: {win_a_price:.2f}+{draw_price:.2f}+{win_b_price:.2f}={total_cost:.4f}"
    )

    return opp

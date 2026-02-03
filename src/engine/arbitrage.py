from __future__ import annotations

import logging
from datetime import UTC, datetime

from src.config import settings
from src.engine.normalizer import FEES, normalize_price
from src.models import ArbitrageOpportunity, MarketPrice, Platform, SportEvent

logger = logging.getLogger(__name__)


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


def _parse_iso_datetime(dt_str: str | None) -> datetime | None:
    """Parse ISO datetime string to datetime object."""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _is_market_expired(market_raw_data: dict, market_type: str) -> bool:
    """Check if market event has already started (game) or expired (futures).

    For game markets: skip if game has already started (stale prices)
    For all markets: skip if close/end time has passed
    """
    now = datetime.now(UTC)

    # Polymarket fields
    game_start = _parse_iso_datetime(market_raw_data.get("game_start_time"))
    end_date = _parse_iso_datetime(market_raw_data.get("end_date"))

    # Kalshi fields
    close_time = _parse_iso_datetime(market_raw_data.get("close_time"))
    expiration_time = _parse_iso_datetime(market_raw_data.get("expiration_time"))

    # For game markets: if game has started, prices are likely stale
    if market_type == "game" and game_start and game_start < now:
        return True

    # Check if market close/end time has passed
    market_end = end_date or close_time or expiration_time
    if market_end and market_end < now:
        return True

    return False


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


def calculate_arbitrage(event: SportEvent) -> ArbitrageOpportunity | None:
    """Check for arbitrage opportunity on a matched event.

    Strategy: Buy YES on one platform and NO on another.
    If total cost < 1.0, there's profit (one side always pays out 1.0).

    We check both directions:
      1) YES on Polymarket + NO on Kalshi
      2) YES on Kalshi + NO on Polymarket
    """
    poly_market = event.markets.get(Platform.POLYMARKET)
    kalshi_market = event.markets.get(Platform.KALSHI)

    if not poly_market or not kalshi_market:
        return None
    if not poly_market.price or not kalshi_market.price:
        return None

    # F0.1: Skip markets where event has already started or market expired
    market_type = poly_market.market_type or kalshi_market.market_type or "game"
    if _is_market_expired(poly_market.raw_data, market_type):
        logger.debug(f"Skipping {event.title}: Polymarket event expired/started")
        return None
    if _is_market_expired(kalshi_market.raw_data, market_type):
        logger.debug(f"Skipping {event.title}: Kalshi event expired/started")
        return None

    pp = normalize_price(poly_market.price, Platform.POLYMARKET)
    kp = normalize_price(kalshi_market.price, Platform.KALSHI)


    # If teams are swapped between platforms, invert Kalshi YES/NO
    # so that YES on both platforms refers to the same team winning
    if event.teams_swapped:
        kp = _invert_price(kp)

    poly_url = poly_market.url or ""
    kalshi_url = kalshi_market.url or ""
    market_subtype = poly_market.raw_data.get("market_subtype", "moneyline")

    # Skip markets with insufficient liquidity — need volume on BOTH platforms
    poly_vol = poly_market.price.volume if poly_market.price else 0
    kalshi_vol = kalshi_market.price.volume if kalshi_market.price else 0
    if (poly_vol or 0) == 0 or (kalshi_vol or 0) == 0:
        return None
    combined_vol = (poly_vol or 0) + (kalshi_vol or 0)
    if combined_vol < 100:
        return None

    # Compute bid-ask spread percentage for liquidity check
    spread_pct = None
    if pp.yes_bid is not None and pp.yes_ask is not None and pp.yes_ask > 0:
        spread_pct = ((pp.yes_ask - pp.yes_bid) / pp.yes_ask) * 100

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

    return best_opp


def calculate_bet_sizes(
    yes_price: float,
    no_price: float,
    yes_platform: Platform,
    no_platform: Platform,
    bankroll: float = 100.0,
) -> dict:
    """Calculate optimal bet sizes for an arbitrage opportunity.

    For a binary arb (YES on one platform + NO on another), allocate
    bankroll proportionally so that guaranteed profit is maximized.

    Returns dict with yes_bet, no_bet, guaranteed_profit, roi_on_capital.
    """
    fee_yes = FEES.get(yes_platform, 0.02)
    fee_no = FEES.get(no_platform, 0.02)

    # Cost per unit including fees
    cost_yes = yes_price * (1 + fee_yes)
    cost_no = no_price * (1 + fee_no)
    total_cost_per_unit = cost_yes + cost_no

    if total_cost_per_unit <= 0 or total_cost_per_unit >= 1.0:
        # No arb or invalid
        units = bankroll / max(total_cost_per_unit, 0.01)
        return {
            "yes_bet": round(cost_yes * units, 2),
            "no_bet": round(cost_no * units, 2),
            "guaranteed_profit": round((1.0 - total_cost_per_unit) * units, 2),
            "roi_on_capital": round((1.0 - total_cost_per_unit) / total_cost_per_unit * 100, 2) if total_cost_per_unit > 0 else 0,
        }

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

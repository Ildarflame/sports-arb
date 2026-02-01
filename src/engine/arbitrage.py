from __future__ import annotations

import logging
from datetime import UTC, datetime

from src.config import settings
from src.engine.normalizer import FEES, normalize_price
from src.models import ArbitrageOpportunity, Platform, SportEvent

logger = logging.getLogger(__name__)


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

    pp = normalize_price(poly_market.price, Platform.POLYMARKET)
    kp = normalize_price(kalshi_market.price, Platform.KALSHI)

    poly_url = poly_market.url or ""
    kalshi_url = kalshi_market.url or ""

    # Skip markets with volume below threshold
    poly_vol = poly_market.price.volume if poly_market.price else 0
    kalshi_vol = kalshi_market.price.volume if kalshi_market.price else 0
    if settings.min_volume > 0:
        if poly_vol == 0 and kalshi_vol == 0:
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

    # Direction 1: Buy YES on Polymarket, buy NO on Kalshi
    cost_1 = pp.yes_price + kp.no_price
    if cost_1 < 1.0:
        gross_profit = 1.0 - cost_1
        # Apply fees
        fee_poly = pp.yes_price * FEES[Platform.POLYMARKET]
        fee_kalshi = kp.no_price * FEES[Platform.KALSHI]
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
                yes_price=pp.yes_price,
                no_price=kp.no_price,
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
                    "direction": "YES@Polymarket + NO@Kalshi",
                    "poly_url": poly_url,
                    "kalshi_url": kalshi_url,
                    "poly_volume": poly_vol,
                    "kalshi_volume": kalshi_vol,
                    "spread_pct": round(spread_pct, 1) if spread_pct is not None else None,
                },
            )

    # Direction 2: Buy YES on Kalshi, buy NO on Polymarket
    cost_2 = kp.yes_price + pp.no_price
    if cost_2 < 1.0:
        gross_profit = 1.0 - cost_2
        fee_kalshi = kp.yes_price * FEES[Platform.KALSHI]
        fee_poly = pp.no_price * FEES[Platform.POLYMARKET]
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
                yes_price=kp.yes_price,
                no_price=pp.no_price,
                total_cost=round(net_cost, 4),
                profit_pct=round(gross_profit * 100, 2),
                roi_after_fees=round(roi, 2),
                found_at=datetime.now(UTC),
                details={
                    "poly_yes": pp.yes_price,
                    "poly_no": pp.no_price,
                    "kalshi_yes": kp.yes_price,
                    "kalshi_no": kp.no_price,
                    "direction": "YES@Kalshi + NO@Polymarket",
                    "poly_url": poly_url,
                    "kalshi_url": kalshi_url,
                    "poly_volume": poly_vol,
                    "kalshi_volume": kalshi_vol,
                    "spread_pct": round(spread_pct, 1) if spread_pct is not None else None,
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

    return best_opp

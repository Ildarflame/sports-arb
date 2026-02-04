"""Liquidity analysis for arbitrage opportunities."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.models import MarketPrice, OrderBookDepth, Platform, SportEvent

logger = logging.getLogger(__name__)


@dataclass
class LiquidityAnalysis:
    """Liquidity profile for an arbitrage opportunity."""

    # Max executable at current best prices (contracts)
    max_contracts_at_best: float

    # Max executable with slippage tolerance (contracts)
    max_contracts_1pct_slip: float
    max_contracts_2pct_slip: float
    max_contracts_5pct_slip: float

    # Dollar amounts (assuming price * contracts)
    max_dollars_at_best: float
    max_dollars_1pct_slip: float
    max_dollars_2pct_slip: float

    # Per-platform breakdown (contracts)
    poly_liquidity: float
    kalshi_liquidity: float

    # Limiting factor
    bottleneck: str  # "polymarket" or "kalshi"

    # Quality score (0-100, higher = more liquid)
    liquidity_score: float

    def to_dict(self) -> dict:
        """Convert to dictionary for storing in opportunity details."""
        return {
            "max_at_best": round(self.max_dollars_at_best, 2),
            "max_1pct_slip": round(self.max_dollars_1pct_slip, 2),
            "max_2pct_slip": round(self.max_dollars_2pct_slip, 2),
            "max_contracts_best": round(self.max_contracts_at_best, 0),
            "max_contracts_1pct": round(self.max_contracts_1pct_slip, 0),
            "max_contracts_2pct": round(self.max_contracts_2pct_slip, 0),
            "poly_contracts": round(self.poly_liquidity, 0),
            "kalshi_contracts": round(self.kalshi_liquidity, 0),
            "bottleneck": self.bottleneck,
            "score": round(self.liquidity_score, 1),
        }


def analyze_arbitrage_liquidity(
    event: SportEvent,
    buy_yes_platform: Platform,
    yes_price: float,
    no_price: float,
) -> LiquidityAnalysis | None:
    """Analyze liquidity for an arbitrage trade.

    Determines how much money can be executed at the arbitrage prices
    by examining order book depth on both platforms.

    Args:
        event: SportEvent with market data from both platforms
        buy_yes_platform: Platform where we buy YES
        yes_price: Target YES execution price
        no_price: Target NO execution price

    Returns:
        LiquidityAnalysis with max executable sizes, or None if insufficient data
    """
    poly_market = event.markets.get(Platform.POLYMARKET)
    kalshi_market = event.markets.get(Platform.KALSHI)

    if not poly_market or not kalshi_market:
        return None

    poly_price = poly_market.price
    kalshi_price = kalshi_market.price

    if not poly_price or not kalshi_price:
        return None

    # Determine which side we're buying on each platform
    if buy_yes_platform == Platform.POLYMARKET:
        # Buy YES on Poly (from asks), Buy NO on Kalshi
        poly_depth = poly_price.yes_depth
        poly_target_price = yes_price
        kalshi_target_price = no_price
    else:
        # Buy YES on Kalshi, Buy NO on Poly (from NO asks)
        poly_depth = poly_price.no_depth
        poly_target_price = no_price
        kalshi_target_price = yes_price

    # Calculate Polymarket liquidity at various slippage levels
    if poly_depth and poly_depth.asks:
        # Volume available at best ask
        poly_at_best = poly_depth.asks[0].size if poly_depth.asks else 0

        # With slippage tolerance
        poly_1pct = poly_depth.max_fillable_at_slippage("buy", poly_target_price, 1.0)
        poly_2pct = poly_depth.max_fillable_at_slippage("buy", poly_target_price, 2.0)
        poly_5pct = poly_depth.max_fillable_at_slippage("buy", poly_target_price, 5.0)

        # Ensure minimums make sense
        poly_1pct = max(poly_1pct, poly_at_best)
        poly_2pct = max(poly_2pct, poly_1pct)
        poly_5pct = max(poly_5pct, poly_2pct)
    else:
        # No depth data - estimate from volume
        poly_volume = poly_price.volume or 0
        # Heuristic: ~1% of daily volume available at best, scaling up with slippage
        poly_at_best = poly_volume * 0.01
        poly_1pct = poly_at_best * 2
        poly_2pct = poly_at_best * 3
        poly_5pct = poly_at_best * 5

    # Kalshi doesn't expose order book depth, estimate from volume
    kalshi_liquidity = _estimate_kalshi_liquidity(kalshi_price)

    # Bottleneck is minimum of both platforms at each level
    max_at_best = min(poly_at_best, kalshi_liquidity)
    max_1pct = min(poly_1pct, kalshi_liquidity * 1.5)
    max_2pct = min(poly_2pct, kalshi_liquidity * 2)
    max_5pct = min(poly_5pct, kalshi_liquidity * 3)

    bottleneck = "polymarket" if poly_at_best < kalshi_liquidity else "kalshi"

    # Convert contracts to dollars
    # Average price per contract (cost to buy YES + NO)
    avg_price = (yes_price + no_price) / 2 if (yes_price + no_price) > 0 else 0.5

    # Liquidity score: 0-100 based on max executable dollars
    # $50 = 20, $200 = 40, $500 = 60, $1000 = 80, $2000+ = 100
    dollars_at_best = max_at_best * avg_price
    if dollars_at_best >= 2000:
        score = 100
    elif dollars_at_best >= 1000:
        score = 80 + (dollars_at_best - 1000) / 1000 * 20
    elif dollars_at_best >= 500:
        score = 60 + (dollars_at_best - 500) / 500 * 20
    elif dollars_at_best >= 200:
        score = 40 + (dollars_at_best - 200) / 300 * 20
    elif dollars_at_best >= 50:
        score = 20 + (dollars_at_best - 50) / 150 * 20
    else:
        score = dollars_at_best / 50 * 20

    return LiquidityAnalysis(
        max_contracts_at_best=max_at_best,
        max_contracts_1pct_slip=max_1pct,
        max_contracts_2pct_slip=max_2pct,
        max_contracts_5pct_slip=max_5pct,
        max_dollars_at_best=dollars_at_best,
        max_dollars_1pct_slip=max_1pct * avg_price,
        max_dollars_2pct_slip=max_2pct * avg_price,
        poly_liquidity=poly_at_best,
        kalshi_liquidity=kalshi_liquidity,
        bottleneck=bottleneck,
        liquidity_score=score,
    )


def _estimate_kalshi_liquidity(price: MarketPrice) -> float:
    """Estimate Kalshi liquidity from volume (no depth data available).

    Kalshi API doesn't expose order book depth, only best bid/ask.
    We estimate based on total volume as a rough proxy.

    Args:
        price: MarketPrice with volume data

    Returns:
        Estimated contracts available at best price
    """
    volume = price.volume or 0

    # Heuristic: ~2% of daily volume is typically available at best price
    # This is conservative - actual liquidity may be higher or lower
    base_estimate = volume * 0.02

    # If we have bid/ask spread, adjust estimate
    # Tighter spread = more liquid market
    if price.yes_bid and price.yes_ask:
        spread = price.yes_ask - price.yes_bid
        if spread < 0.02:  # Very tight spread (<2 cents)
            base_estimate *= 1.5
        elif spread < 0.05:  # Tight spread (<5 cents)
            base_estimate *= 1.2
        elif spread > 0.10:  # Wide spread (>10 cents)
            base_estimate *= 0.5

    # Minimum estimate of 10 contracts if there's any volume
    if volume > 0:
        base_estimate = max(base_estimate, 10)

    return base_estimate


def calculate_slippage_cost(
    depth: OrderBookDepth,
    side: str,
    size: float,
) -> tuple[float, float, float]:
    """Calculate the cost of executing a given size with slippage.

    Args:
        depth: Order book depth
        side: "buy" or "sell"
        size: Number of contracts to execute

    Returns:
        (total_cost, average_price, slippage_pct from best price)
    """
    if not depth:
        return 0, 0, 0

    best_price = depth.best_ask if side == "buy" else depth.best_bid
    if not best_price or best_price <= 0:
        return 0, 0, 0

    total_cost, avg_price = depth.cost_to_fill(side, size)

    if avg_price > 0 and best_price > 0:
        if side == "buy":
            slippage_pct = ((avg_price - best_price) / best_price) * 100
        else:
            slippage_pct = ((best_price - avg_price) / best_price) * 100
    else:
        slippage_pct = 0

    return total_cost, avg_price, slippage_pct

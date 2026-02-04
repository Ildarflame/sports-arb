from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Platform(str, Enum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


class OrderBookLevel(BaseModel):
    """Single level in order book."""

    price: float
    size: float  # Number of contracts available at this price


class OrderBookDepth(BaseModel):
    """Full order book depth for a market."""

    bids: list[OrderBookLevel] = Field(default_factory=list)  # Sorted by price descending (best first)
    asks: list[OrderBookLevel] = Field(default_factory=list)  # Sorted by price ascending (best first)

    @property
    def best_bid(self) -> float | None:
        """Best (highest) bid price."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        """Best (lowest) ask price."""
        return self.asks[0].price if self.asks else None

    @property
    def spread_pct(self) -> float | None:
        """Spread as percentage of ask price."""
        if self.best_bid is not None and self.best_ask is not None and self.best_ask > 0:
            return ((self.best_ask - self.best_bid) / self.best_ask) * 100
        return None

    @property
    def total_bid_volume(self) -> float:
        """Total volume across all bid levels."""
        return sum(level.size for level in self.bids)

    @property
    def total_ask_volume(self) -> float:
        """Total volume across all ask levels."""
        return sum(level.size for level in self.asks)

    def volume_at_price(self, side: str, max_price: float) -> float:
        """Total volume available at or better than max_price.

        Args:
            side: "buy" (uses asks) or "sell" (uses bids)
            max_price: Maximum price willing to pay (for buy) or minimum (for sell)

        Returns:
            Total contracts available within price limit.
        """
        if side == "buy":
            # Buying from asks - want prices <= max_price
            return sum(level.size for level in self.asks if level.price <= max_price)
        else:
            # Selling to bids - want prices >= max_price
            return sum(level.size for level in self.bids if level.price >= max_price)

    def cost_to_fill(self, side: str, size: float) -> tuple[float, float]:
        """Calculate cost to fill `size` contracts.

        Args:
            side: "buy" (uses asks) or "sell" (uses bids)
            size: Number of contracts to fill

        Returns:
            (total_cost, average_price). If insufficient liquidity,
            returns cost/avg for available amount.
        """
        levels = self.asks if side == "buy" else self.bids
        remaining = size
        total_cost = 0.0
        filled = 0.0

        for level in levels:
            if remaining <= 0:
                break
            fill_at_level = min(remaining, level.size)
            total_cost += fill_at_level * level.price
            filled += fill_at_level
            remaining -= fill_at_level

        avg_price = total_cost / filled if filled > 0 else 0
        return total_cost, avg_price

    def max_fillable_at_slippage(self, side: str, best_price: float, max_slippage_pct: float) -> float:
        """Calculate maximum fillable size within slippage tolerance.

        Args:
            side: "buy" or "sell"
            best_price: Reference price (best bid or ask)
            max_slippage_pct: Maximum acceptable slippage percentage

        Returns:
            Maximum contracts fillable within slippage limit.
        """
        if best_price <= 0:
            return 0.0

        if side == "buy":
            max_price = best_price * (1 + max_slippage_pct / 100)
            return self.volume_at_price("buy", max_price)
        else:
            min_price = best_price * (1 - max_slippage_pct / 100)
            return self.volume_at_price("sell", min_price)


class MarketPrice(BaseModel):
    yes_price: float = Field(ge=0, le=1, description="Implied probability for YES")
    no_price: float = Field(ge=0, le=1, description="Implied probability for NO")
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    volume: float = 0
    last_updated: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Order book depth (None if not available)
    yes_depth: OrderBookDepth | None = None
    no_depth: OrderBookDepth | None = None


class Market(BaseModel):
    platform: Platform
    market_id: str
    event_id: str
    title: str
    team_a: str
    team_b: str
    category: str = "sports"
    market_type: str = ""  # "game" for daily matches, "futures" for championships/awards
    sport: str = ""  # e.g. "soccer", "nba", "nhl", "tennis", "esports"
    game_date: date | None = None  # date of the game (for daily matches)
    event_group: str = ""  # tournament/award identifier for futures matching
    line: float | None = None  # spread/total line value (e.g., -3.5, 220.5)
    map_number: int | None = None  # esports map number (1, 2, 3, etc.)
    url: str = ""
    price: MarketPrice | None = None
    raw_data: dict = Field(default_factory=dict)


class SportEvent(BaseModel):
    id: str = ""
    title: str
    team_a: str
    team_b: str
    start_time: datetime | None = None
    category: str = "sports"
    markets: dict[Platform, Market] = Field(default_factory=dict)
    matched: bool = False
    teams_swapped: bool = False  # True = Poly team_a corresponds to Kalshi team_b


class ThreeWayGroup(BaseModel):
    """Group of 3-way markets (Win A, Draw, Win B) for a soccer match."""
    team_a: str
    team_b: str
    game_date: date | None = None
    sport: str = "soccer"
    # Markets by outcome and platform
    poly_win_a: Market | None = None
    poly_draw: Market | None = None
    poly_win_b: Market | None = None
    kalshi_win_a: Market | None = None
    kalshi_draw: Market | None = None
    kalshi_win_b: Market | None = None


class ArbitrageOpportunity(BaseModel):
    id: str = ""
    event_title: str
    team_a: str
    team_b: str
    platform_buy_yes: Platform
    platform_buy_no: Platform
    yes_price: float
    no_price: float
    total_cost: float
    profit_pct: float
    roi_after_fees: float
    found_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    still_active: bool = True
    details: dict = Field(default_factory=dict)

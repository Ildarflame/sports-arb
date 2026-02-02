from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Platform(str, Enum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


class MarketPrice(BaseModel):
    yes_price: float = Field(ge=0, le=1, description="Implied probability for YES")
    no_price: float = Field(ge=0, le=1, description="Implied probability for NO")
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    volume: float = 0
    last_updated: datetime = Field(default_factory=lambda: datetime.now(UTC))


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

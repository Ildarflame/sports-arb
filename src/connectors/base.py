from __future__ import annotations

import abc
from typing import AsyncIterator

from src.models import Market, MarketPrice


class BaseConnector(abc.ABC):
    """Abstract base class for market connectors."""

    @abc.abstractmethod
    async def connect(self) -> None:
        """Initialize connection / authenticate."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Clean up resources."""

    @abc.abstractmethod
    async def fetch_sports_events(self) -> list[Market]:
        """Fetch current sports/esports markets."""

    @abc.abstractmethod
    async def fetch_price(self, market_id: str) -> MarketPrice | None:
        """Fetch current price for a market."""

    async def subscribe_prices(self, market_ids: list[str]) -> AsyncIterator[tuple[str, MarketPrice]]:
        """Subscribe to real-time price updates. Optional — not all connectors support WS."""
        raise NotImplementedError
        yield  # make it an async generator

"""Tests for Kalshi trading methods."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.connectors.kalshi import KalshiConnector


@pytest.fixture
def kalshi():
    """Create Kalshi connector."""
    return KalshiConnector()


@pytest.mark.asyncio
async def test_get_balance(kalshi):
    """Should fetch account balance."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"balance": 1050}  # cents
    mock_response.raise_for_status = MagicMock()

    kalshi._http = AsyncMock()
    kalshi._http.get = AsyncMock(return_value=mock_response)

    balance = await kalshi.get_balance()
    assert balance == 10.50  # converted to dollars


@pytest.mark.asyncio
async def test_place_order_buy_yes(kalshi):
    """Should place buy YES order."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "order": {
            "order_id": "ord_123",
            "status": "resting",
            "yes_price": 51,
            "count_total": 2,
        }
    }
    mock_response.raise_for_status = MagicMock()

    kalshi._http = AsyncMock()
    kalshi._http.post = AsyncMock(return_value=mock_response)

    result = await kalshi.place_order(
        ticker="KXNBA-123",
        side="yes",
        action="buy",
        count=2,
        price_cents=51,
    )
    assert result["order_id"] == "ord_123"


@pytest.mark.asyncio
async def test_place_order_buy_no(kalshi):
    """Should place buy NO order."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "order": {
            "order_id": "ord_456",
            "status": "filled",
            "no_price": 48,
            "count_total": 2,
        }
    }
    mock_response.raise_for_status = MagicMock()

    kalshi._http = AsyncMock()
    kalshi._http.post = AsyncMock(return_value=mock_response)

    result = await kalshi.place_order(
        ticker="KXNBA-123",
        side="no",
        action="buy",
        count=2,
        price_cents=48,
    )
    assert result["order_id"] == "ord_456"


@pytest.mark.asyncio
async def test_get_order_status(kalshi):
    """Should fetch order status."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "order": {
            "order_id": "ord_123",
            "status": "filled",
            "count_filled": 2,
            "count_total": 2,
        }
    }
    mock_response.raise_for_status = MagicMock()

    kalshi._http = AsyncMock()
    kalshi._http.get = AsyncMock(return_value=mock_response)

    result = await kalshi.get_order("ord_123")
    assert result["status"] == "filled"
    assert result["count_filled"] == 2

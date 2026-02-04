"""Tests for Polymarket trading methods."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.connectors.polymarket import PolymarketConnector


@pytest.fixture
def polymarket():
    """Create Polymarket connector."""
    return PolymarketConnector()


def test_trading_client_initialization(polymarket):
    """Trading client should be None until initialized."""
    assert polymarket._trading_client is None


@pytest.mark.asyncio
async def test_get_balance(polymarket):
    """Should fetch USDC balance via get_balance_allowance."""
    # Mock the trading client
    mock_client = MagicMock()
    mock_client.get_balance_allowance.return_value = {
        "balance": "10500000",  # 6 decimals for USDC, as string
        "allowances": {},
    }
    polymarket._trading_client = mock_client

    balance = await polymarket.get_balance()
    assert balance == 10.50  # converted to dollars
    mock_client.get_balance_allowance.assert_called_once()


@pytest.mark.asyncio
async def test_place_order(polymarket):
    """Should place order via py-clob-client."""
    mock_client = MagicMock()
    mock_client.create_and_post_order.return_value = {
        "success": True,
        "orderID": "poly_123",
    }
    polymarket._trading_client = mock_client

    result = await polymarket.place_order(
        token_id="12345",
        side="BUY",
        price=0.51,
        size=2.0,
    )

    assert result["success"]
    assert result["orderID"] == "poly_123"


@pytest.mark.asyncio
async def test_place_market_order(polymarket):
    """Should place FOK market order."""
    mock_client = MagicMock()
    mock_client.create_market_order.return_value = MagicMock()
    mock_client.post_order.return_value = {
        "success": True,
        "orderID": "poly_456",
    }
    polymarket._trading_client = mock_client

    result = await polymarket.place_market_order(
        token_id="12345",
        side="BUY",
        amount=2.0,
    )

    assert result["success"]

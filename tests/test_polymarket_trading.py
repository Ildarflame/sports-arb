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
async def test_place_order_gtc(polymarket):
    """Should place GTC limit order via py-clob-client."""
    mock_client = MagicMock()
    mock_signed_order = MagicMock()
    mock_client.create_order.return_value = mock_signed_order
    mock_client.post_order.return_value = {
        "success": True,
        "orderID": "poly_123",
    }
    polymarket._trading_client = mock_client

    result = await polymarket.place_order(
        token_id="12345",
        side="BUY",
        price=0.51,
        size=2.0,
        order_type="GTC",
    )

    assert result["success"]
    assert result["orderID"] == "poly_123"
    mock_client.create_order.assert_called_once()
    mock_client.post_order.assert_called_once_with(mock_signed_order, "GTC")


@pytest.mark.asyncio
async def test_place_order_fok(polymarket):
    """Should place FOK market order via py-clob-client."""
    mock_client = MagicMock()
    mock_signed_order = MagicMock()
    mock_client.create_market_order.return_value = mock_signed_order
    mock_client.post_order.return_value = {
        "success": True,
        "orderID": "poly_456",
        "matchedAmount": "2.0",
        "avgPrice": "0.51",
    }
    polymarket._trading_client = mock_client

    result = await polymarket.place_order(
        token_id="12345",
        side="BUY",
        price=0.51,
        size=2.0,
        order_type="FOK",
    )

    assert result["success"]
    assert result["orderID"] == "poly_456"
    mock_client.create_market_order.assert_called_once()
    mock_client.post_order.assert_called_once_with(mock_signed_order, "FOK")


@pytest.mark.asyncio
async def test_place_order_error(polymarket):
    """Should handle order errors gracefully."""
    mock_client = MagicMock()
    mock_client.create_order.side_effect = Exception("Network error")
    polymarket._trading_client = mock_client

    result = await polymarket.place_order(
        token_id="12345",
        side="BUY",
        price=0.51,
        size=2.0,
    )

    assert not result["success"]
    assert "Network error" in result["errorMsg"]

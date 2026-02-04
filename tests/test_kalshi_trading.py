"""Tests for Kalshi trading methods."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.connectors.kalshi import KalshiConnector


@pytest.fixture
def kalshi():
    """Create Kalshi connector with mocked credentials."""
    connector = KalshiConnector()
    connector._member_id = "test_member"
    connector._http = MagicMock()  # Pretend we're connected
    return connector


@pytest.mark.asyncio
async def test_get_balance(kalshi):
    """Should fetch account balance."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"balance": 1050}  # cents
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )
        balance = await kalshi.get_balance()

    assert balance == 10.50  # converted to dollars


@pytest.mark.asyncio
async def test_place_order_buy_yes(kalshi):
    """Should place buy YES order."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {
        "order": {
            "order_id": "ord_123",
            "status": "resting",
            "ticker": "KXNBA-123",
            "side": "yes",
            "action": "buy",
        }
    }

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=mock_response
        )
        result = await kalshi.place_order(
            ticker="KXNBA-123",
            side="yes",
            action="buy",
            count=2,
            price_cents=51,
        )

    assert result["order_id"] == "ord_123"
    assert result["status"] == "resting"


@pytest.mark.asyncio
async def test_place_order_buy_no(kalshi):
    """Should place buy NO order."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {
        "order": {
            "order_id": "ord_456",
            "status": "filled",
            "ticker": "KXNBA-123",
            "side": "no",
            "action": "buy",
        }
    }

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=mock_response
        )
        result = await kalshi.place_order(
            ticker="KXNBA-123",
            side="no",
            action="buy",
            count=2,
            price_cents=48,
        )

    assert result["order_id"] == "ord_456"
    assert result["status"] == "filled"


@pytest.mark.asyncio
async def test_place_order_error(kalshi):
    """Should handle order errors gracefully."""
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.content = b'{"message": "Insufficient balance"}'
    mock_response.json.return_value = {"message": "Insufficient balance"}

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=mock_response
        )
        result = await kalshi.place_order(
            ticker="KXNBA-123",
            side="yes",
            action="buy",
            count=2,
            price_cents=51,
        )

    assert result["status"] == "failed"
    assert "Insufficient balance" in result["error"]

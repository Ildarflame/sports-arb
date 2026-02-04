"""Trade logger for persisting executor trades and positions."""

from __future__ import annotations

import logging
from datetime import datetime, UTC
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.db import Database
    from src.executor.ws_handler import ExecutorWSHandler

logger = logging.getLogger(__name__)


class TradeLogger:
    """Logs trades and manages positions with DB persistence.

    Integrates with WebSocket handler for real-time dashboard updates.
    """

    def __init__(self, db: Database, ws_handler: ExecutorWSHandler | None = None):
        self._db = db
        self._ws = ws_handler

    def set_ws_handler(self, ws_handler: ExecutorWSHandler) -> None:
        """Set WebSocket handler after initialization."""
        self._ws = ws_handler

    async def log_trade(
        self,
        event_title: str,
        status: str,
        bet_size: float,
        pnl: float = 0,
        roi: float | None = None,
        poly_order_id: str | None = None,
        kalshi_order_id: str | None = None,
        details: dict | None = None,
    ) -> int:
        """Log a trade execution.

        Args:
            event_title: Name of the event (e.g., "Lakers vs Celtics")
            status: Trade status (SUCCESS, FAILED, ROLLED_BACK, PARTIAL)
            bet_size: Amount wagered
            pnl: Realized P&L (negative for losses)
            roi: Expected ROI percentage
            poly_order_id: Polymarket order ID
            kalshi_order_id: Kalshi order ID
            details: Additional trade details

        Returns:
            Trade ID from database.
        """
        trade_id = await self._db.save_executor_trade(
            event_title=event_title,
            status=status,
            bet_size=bet_size,
            pnl=pnl,
            roi=roi,
            poly_order_id=poly_order_id,
            kalshi_order_id=kalshi_order_id,
            details=details,
        )

        logger.info(f"Logged trade: {event_title} | {status} | ${bet_size} | PnL=${pnl}")

        # Broadcast to WebSocket clients
        if self._ws:
            await self._ws.broadcast_trade_event(
                event_title=event_title,
                status=status,
                bet_size=bet_size,
                pnl=pnl,
                roi=roi,
                details=details,
            )

        return trade_id

    async def open_position(
        self,
        event_key: str,
        event_title: str,
        poly_side: str,
        poly_price: float,
        poly_contracts: int,
        kalshi_side: str,
        kalshi_price: float,
        kalshi_contracts: int,
    ) -> int:
        """Record a new open position.

        Args:
            event_key: Unique key for the position (usually kalshi_ticker)
            event_title: Event name
            poly_side: YES or NO
            poly_price: Average fill price on Polymarket
            poly_contracts: Number of contracts on Polymarket
            kalshi_side: yes or no
            kalshi_price: Average fill price on Kalshi
            kalshi_contracts: Number of contracts on Kalshi

        Returns:
            Position ID from database.
        """
        position_id = await self._db.save_executor_position(
            event_key=event_key,
            event_title=event_title,
            poly_side=poly_side,
            poly_price=poly_price,
            poly_contracts=poly_contracts,
            kalshi_side=kalshi_side,
            kalshi_price=kalshi_price,
            kalshi_contracts=kalshi_contracts,
        )

        logger.info(f"Opened position: {event_title} | {event_key}")

        # Broadcast to WebSocket clients
        if self._ws:
            await self._ws.broadcast_position_opened({
                "event_key": event_key,
                "event_title": event_title,
                "poly_side": poly_side,
                "poly_price": poly_price,
                "poly_contracts": poly_contracts,
                "kalshi_side": kalshi_side,
                "kalshi_price": kalshi_price,
                "kalshi_contracts": kalshi_contracts,
                "opened_at": datetime.now(UTC).isoformat(),
            })

        return position_id

    async def close_position(self, event_key: str, status: str = "closed") -> None:
        """Close an open position.

        Args:
            event_key: Position identifier
            status: New status (closed, expired, settled)
        """
        await self._db.close_executor_position(event_key, status)
        logger.info(f"Closed position: {event_key} | {status}")

        # Broadcast to WebSocket clients
        if self._ws:
            await self._ws.broadcast_position_closed(event_key)

    async def get_open_positions(self) -> list[dict]:
        """Get all open positions."""
        return await self._db.get_executor_positions(status="open")

    async def get_recent_trades(self, limit: int = 50) -> list[dict]:
        """Get recent trades."""
        return await self._db.get_executor_trades(limit=limit)

    async def get_daily_stats(self) -> dict:
        """Get today's trading statistics."""
        return await self._db.get_daily_executor_stats()

    async def broadcast_balances(self, poly: float, kalshi: float) -> None:
        """Broadcast balance update to dashboard."""
        if self._ws:
            await self._ws.broadcast_balance_update(poly, kalshi)

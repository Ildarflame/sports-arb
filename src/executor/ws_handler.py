"""WebSocket handler for executor real-time updates."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from src.db import Database
    from src.executor.settings_manager import ExecutorSettingsManager

logger = logging.getLogger(__name__)


class ExecutorWSHandler:
    """Manages WebSocket connections for executor dashboard.

    Handles bidirectional communication:
    - Server → Client: init, balance_update, trade_event, position_*, settings_changed, status_changed
    - Client → Server: toggle_enabled, update_settings, close_position
    """

    def __init__(
        self,
        settings_manager: ExecutorSettingsManager,
        db: Database,
        poly_connector=None,
        kalshi_connector=None,
    ):
        self.settings = settings_manager
        self.db = db
        self.poly = poly_connector
        self.kalshi = kalshi_connector
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept new WebSocket connection and send initial state."""
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info(f"Executor WS connected, total: {len(self._connections)}")

        # Send initial state
        try:
            init_data = await self._get_init_state()
            await websocket.send_json({"type": "init", "data": init_data})
        except Exception as e:
            logger.error(f"Failed to send init state: {e}")

    async def disconnect(self, websocket: WebSocket) -> None:
        """Handle WebSocket disconnection."""
        async with self._lock:
            self._connections.discard(websocket)
        logger.info(f"Executor WS disconnected, total: {len(self._connections)}")

    async def handle_message(self, websocket: WebSocket, message: dict) -> None:
        """Process incoming message from client."""
        action = message.get("action")

        try:
            if action == "toggle_enabled":
                value = message.get("value")
                if value is not None:
                    await self.settings.set_enabled(bool(value))
                else:
                    await self.settings.toggle_enabled()
                await self.broadcast_status_changed()

            elif action == "update_settings":
                settings_data = message.get("settings", {})
                await self.settings.update(**settings_data)
                await self.broadcast_settings_changed()

            elif action == "close_position":
                position_id = message.get("position_id")
                if position_id:
                    await self.db.close_executor_position(position_id)
                    await self.broadcast_position_closed(position_id)

            else:
                await websocket.send_json({
                    "type": "error",
                    "message": f"Unknown action: {action}",
                })

        except Exception as e:
            logger.error(f"Error handling WS message: {e}")
            await websocket.send_json({
                "type": "error",
                "message": str(e),
            })

    async def handle_connection(self, websocket: WebSocket) -> None:
        """Main connection handler loop."""
        await self.connect(websocket)
        try:
            while True:
                data = await websocket.receive_json()
                await self.handle_message(websocket, data)
        except WebSocketDisconnect:
            await self.disconnect(websocket)
        except Exception as e:
            logger.error(f"WS error: {e}")
            await self.disconnect(websocket)

    async def _get_init_state(self) -> dict:
        """Build initial state for new connection."""
        settings = self.settings.get()

        # Get balances
        balances = {"poly": 0.0, "kalshi": 0.0}
        try:
            if self.poly:
                balances["poly"] = await self.poly.get_balance()
            if self.kalshi:
                balances["kalshi"] = await self.kalshi.get_balance()
        except Exception as e:
            logger.warning(f"Failed to fetch balances: {e}")

        # Get daily stats
        stats = await self.db.get_daily_executor_stats()

        # Get open positions
        positions = await self.db.get_executor_positions(status="open")

        # Get recent trades
        trades = await self.db.get_executor_trades(limit=20)

        return {
            "enabled": settings.enabled,
            "settings": settings.to_dict(),
            "balances": balances,
            "stats": {
                "trades": stats.get("trades", 0),
                "pnl": stats.get("pnl", 0),
                "successful": stats.get("successful", 0),
                "rolled_back": stats.get("rolled_back", 0),
                "failed": stats.get("failed", 0),
            },
            "positions": positions,
            "trades": trades,
        }

    async def broadcast(self, message: dict) -> None:
        """Send message to all connected clients."""
        if not self._connections:
            return

        disconnected = set()
        async with self._lock:
            for ws in self._connections:
                try:
                    await ws.send_json(message)
                except Exception:
                    disconnected.add(ws)

            # Clean up disconnected clients
            self._connections -= disconnected

    async def broadcast_balance_update(self, poly: float, kalshi: float) -> None:
        """Broadcast balance update to all clients."""
        await self.broadcast({
            "type": "balance_update",
            "data": {"poly": poly, "kalshi": kalshi},
        })

    async def broadcast_trade_event(
        self,
        event_title: str,
        status: str,
        bet_size: float,
        pnl: float = 0,
        roi: float | None = None,
        details: dict | None = None,
    ) -> None:
        """Broadcast trade event to all clients."""
        await self.broadcast({
            "type": "trade_event",
            "data": {
                "event": event_title,
                "status": status,
                "bet_size": bet_size,
                "pnl": pnl,
                "roi": roi,
                "details": details or {},
            },
        })

    async def broadcast_position_opened(self, position: dict) -> None:
        """Broadcast new position to all clients."""
        await self.broadcast({
            "type": "position_opened",
            "data": position,
        })

    async def broadcast_position_closed(self, event_key: str) -> None:
        """Broadcast position closed to all clients."""
        await self.broadcast({
            "type": "position_closed",
            "data": {"event_key": event_key},
        })

    async def broadcast_settings_changed(self) -> None:
        """Broadcast settings change to all clients."""
        settings = self.settings.get()
        await self.broadcast({
            "type": "settings_changed",
            "data": settings.to_dict(),
        })

    async def broadcast_status_changed(self) -> None:
        """Broadcast enabled status change to all clients."""
        await self.broadcast({
            "type": "status_changed",
            "data": {"enabled": self.settings.enabled},
        })

    @property
    def connection_count(self) -> int:
        """Number of active connections."""
        return len(self._connections)

"""Position manager for tracking open arbitrage positions."""

from __future__ import annotations

import logging
from datetime import datetime, UTC, date

import aiosqlite

from src.executor.models import OpenPosition

logger = logging.getLogger(__name__)

_POSITIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    event_title TEXT NOT NULL,
    team_a TEXT NOT NULL,
    team_b TEXT NOT NULL,
    poly_side TEXT NOT NULL,
    poly_amount REAL NOT NULL,
    poly_contracts REAL NOT NULL,
    poly_avg_price REAL NOT NULL,
    poly_order_id TEXT DEFAULT '',
    kalshi_side TEXT NOT NULL,
    kalshi_amount REAL NOT NULL,
    kalshi_contracts INTEGER NOT NULL,
    kalshi_avg_price REAL NOT NULL,
    kalshi_order_id TEXT DEFAULT '',
    arb_type TEXT NOT NULL,
    expected_roi REAL NOT NULL,
    opened_at TEXT NOT NULL,
    status TEXT DEFAULT 'open',
    settled_at TEXT,
    actual_pnl REAL,
    winning_side TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status, opened_at);
"""


class PositionManager:
    """Manages open positions and settlement tracking."""

    def __init__(self, db_path: str = ""):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Initialize database connection."""
        if not self.db_path:
            from src.config import settings
            self.db_path = settings.db_path
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_POSITIONS_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def save_position(self, pos: OpenPosition) -> None:
        """Save new position to database."""
        await self._db.execute(
            """
            INSERT OR REPLACE INTO positions (
                id, event_title, team_a, team_b,
                poly_side, poly_amount, poly_contracts, poly_avg_price, poly_order_id,
                kalshi_side, kalshi_amount, kalshi_contracts, kalshi_avg_price, kalshi_order_id,
                arb_type, expected_roi, opened_at, status, settled_at, actual_pnl, winning_side
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pos.id, pos.event_title, pos.team_a, pos.team_b,
                pos.poly_side, pos.poly_amount, pos.poly_contracts, pos.poly_avg_price, pos.poly_order_id,
                pos.kalshi_side, pos.kalshi_amount, pos.kalshi_contracts, pos.kalshi_avg_price, pos.kalshi_order_id,
                pos.arb_type, pos.expected_roi,
                pos.opened_at.isoformat() if pos.opened_at else datetime.now(UTC).isoformat(),
                pos.status,
                pos.settled_at.isoformat() if pos.settled_at else None,
                pos.actual_pnl,
                pos.winning_side,
            ),
        )
        await self._db.commit()

    async def get_position(self, position_id: str) -> OpenPosition | None:
        """Get position by ID."""
        cursor = await self._db.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_position(row)

    async def get_open_positions(self) -> list[OpenPosition]:
        """Get all open (unsettled) positions."""
        cursor = await self._db.execute(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY opened_at"
        )
        rows = await cursor.fetchall()
        return [self._row_to_position(row) for row in rows]

    async def settle_position(
        self,
        position_id: str,
        actual_pnl: float,
        winning_side: str,
    ) -> None:
        """Mark position as settled with actual P&L."""
        await self._db.execute(
            """
            UPDATE positions
            SET status = 'settled',
                settled_at = ?,
                actual_pnl = ?,
                winning_side = ?
            WHERE id = ?
            """,
            (datetime.now(UTC).isoformat(), actual_pnl, winning_side, position_id),
        )
        await self._db.commit()

    async def get_daily_stats(self, day: date | None = None) -> dict:
        """Get statistics for a specific day."""
        if day is None:
            day = date.today()

        day_start = f"{day.isoformat()}T00:00:00"
        day_end = f"{day.isoformat()}T23:59:59"

        # Count trades
        cursor = await self._db.execute(
            """
            SELECT COUNT(*) as trades,
                   SUM(CASE WHEN status = 'settled' THEN 1 ELSE 0 END) as settled,
                   SUM(CASE WHEN status = 'partial' THEN 1 ELSE 0 END) as partial,
                   COALESCE(SUM(actual_pnl), 0) as pnl
            FROM positions
            WHERE opened_at >= ? AND opened_at <= ?
            """,
            (day_start, day_end),
        )
        row = await cursor.fetchone()

        return {
            "trades": row["trades"] or 0,
            "settled": row["settled"] or 0,
            "partial": row["partial"] or 0,
            "pnl": row["pnl"] or 0.0,
        }

    def _row_to_position(self, row: aiosqlite.Row) -> OpenPosition:
        """Convert database row to OpenPosition."""
        return OpenPosition(
            id=row["id"],
            event_title=row["event_title"],
            team_a=row["team_a"],
            team_b=row["team_b"],
            poly_side=row["poly_side"],
            poly_amount=row["poly_amount"],
            poly_contracts=row["poly_contracts"],
            poly_avg_price=row["poly_avg_price"],
            poly_order_id=row["poly_order_id"] or "",
            kalshi_side=row["kalshi_side"],
            kalshi_amount=row["kalshi_amount"],
            kalshi_contracts=row["kalshi_contracts"],
            kalshi_avg_price=row["kalshi_avg_price"],
            kalshi_order_id=row["kalshi_order_id"] or "",
            arb_type=row["arb_type"],
            expected_roi=row["expected_roi"],
            opened_at=datetime.fromisoformat(row["opened_at"]) if row["opened_at"] else datetime.now(UTC),
            status=row["status"],
            settled_at=datetime.fromisoformat(row["settled_at"]) if row["settled_at"] else None,
            actual_pnl=row["actual_pnl"],
            winning_side=row["winning_side"],
        )

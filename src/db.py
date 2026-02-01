from __future__ import annotations

import json
import uuid
from datetime import datetime

import aiosqlite

from src.config import settings
from src.models import ArbitrageOpportunity

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id TEXT PRIMARY KEY,
    event_title TEXT NOT NULL,
    team_a TEXT NOT NULL,
    team_b TEXT NOT NULL,
    platform_buy_yes TEXT NOT NULL,
    platform_buy_no TEXT NOT NULL,
    yes_price REAL NOT NULL,
    no_price REAL NOT NULL,
    total_cost REAL NOT NULL,
    profit_pct REAL NOT NULL,
    roi_after_fees REAL NOT NULL,
    found_at TEXT NOT NULL,
    still_active INTEGER DEFAULT 1,
    details TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_opps_active ON opportunities(still_active, found_at);
"""


class Database:
    def __init__(self, db_path: str = ""):
        self.db_path = db_path or settings.db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def commit(self) -> None:
        """Explicit commit — call once at end of scan loop."""
        if self._db:
            await self._db.commit()

    async def find_active_by_key(
        self, team_a: str, platform_yes: str, platform_no: str,
    ) -> dict | None:
        """Find an existing active opportunity for the same team/platform pair."""
        cursor = await self._db.execute(
            """SELECT * FROM opportunities
               WHERE still_active = 1
                 AND team_a = ? AND platform_buy_yes = ? AND platform_buy_no = ?
               ORDER BY found_at DESC LIMIT 1""",
            (team_a, platform_yes, platform_no),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def save_opportunity(self, opp: ArbitrageOpportunity) -> str:
        # Dedup: update existing active arb for same key instead of inserting
        existing = await self.find_active_by_key(
            opp.team_a, opp.platform_buy_yes.value, opp.platform_buy_no.value,
        )
        if existing:
            opp.id = existing["id"]
            await self._db.execute(
                """UPDATE opportunities
                   SET yes_price = ?, no_price = ?, total_cost = ?,
                       profit_pct = ?, roi_after_fees = ?,
                       found_at = ?, details = ?
                   WHERE id = ?""",
                (
                    opp.yes_price, opp.no_price, opp.total_cost,
                    opp.profit_pct, opp.roi_after_fees,
                    opp.found_at.isoformat(), json.dumps(opp.details),
                    opp.id,
                ),
            )
            return opp.id

        if not opp.id:
            opp.id = uuid.uuid4().hex[:12]
        await self._db.execute(
            """INSERT OR REPLACE INTO opportunities
               (id, event_title, team_a, team_b, platform_buy_yes, platform_buy_no,
                yes_price, no_price, total_cost, profit_pct, roi_after_fees,
                found_at, still_active, details)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                opp.id, opp.event_title, opp.team_a, opp.team_b,
                opp.platform_buy_yes.value, opp.platform_buy_no.value,
                opp.yes_price, opp.no_price, opp.total_cost,
                opp.profit_pct, opp.roi_after_fees,
                opp.found_at.isoformat(), int(opp.still_active),
                json.dumps(opp.details),
            ),
        )
        return opp.id

    async def get_active_opportunities(self, limit: int = 50) -> list[dict]:
        cursor = await self._db.execute(
            """SELECT * FROM opportunities
               WHERE still_active = 1
               ORDER BY found_at DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_all_opportunities(self, limit: int = 200) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM opportunities ORDER BY found_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_active_opp_keys(self) -> dict[tuple[str, str, str], str]:
        """Return mapping of (team_a, platform_yes, platform_no) -> opp_id for active opps."""
        cursor = await self._db.execute(
            "SELECT id, team_a, platform_buy_yes, platform_buy_no FROM opportunities WHERE still_active = 1"
        )
        rows = await cursor.fetchall()
        return {
            (r["team_a"], r["platform_buy_yes"], r["platform_buy_no"]): r["id"]
            for r in rows
        }

    async def deactivate_opportunity(self, opp_id: str) -> None:
        await self._db.execute(
            "UPDATE opportunities SET still_active = 0 WHERE id = ?", (opp_id,)
        )

    async def deactivate_by_key(
        self, team_a: str, platform_yes: str, platform_no: str,
    ) -> int:
        """Deactivate ALL active opportunities matching the given key."""
        cursor = await self._db.execute(
            """UPDATE opportunities SET still_active = 0
               WHERE still_active = 1
                 AND team_a = ? AND platform_buy_yes = ? AND platform_buy_no = ?""",
            (team_a, platform_yes, platform_no),
        )
        return cursor.rowcount

    async def cleanup_old(self, days: int = 7) -> int:
        """Delete inactive opportunities older than `days` days."""
        cursor = await self._db.execute(
            "DELETE FROM opportunities WHERE still_active = 0 AND found_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        return cursor.rowcount


db = Database()

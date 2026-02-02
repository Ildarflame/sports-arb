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
    details TEXT DEFAULT '{}',
    sport TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_opps_active ON opportunities(still_active, found_at);
"""

# Migration: add sport column if missing (for existing databases)
_MIGRATION_ADD_SPORT = "ALTER TABLE opportunities ADD COLUMN sport TEXT DEFAULT ''"


class Database:
    def __init__(self, db_path: str = ""):
        self.db_path = db_path or settings.db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        # Run migrations for existing databases
        try:
            await self._db.execute(_MIGRATION_ADD_SPORT)
        except Exception:
            pass  # Column already exists
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

    async def save_opportunity(self, opp: ArbitrageOpportunity, sport: str = "") -> str:
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
                       found_at = ?, details = ?, sport = ?
                   WHERE id = ?""",
                (
                    opp.yes_price, opp.no_price, opp.total_cost,
                    opp.profit_pct, opp.roi_after_fees,
                    opp.found_at.isoformat(), json.dumps(opp.details),
                    sport, opp.id,
                ),
            )
            return opp.id

        if not opp.id:
            opp.id = uuid.uuid4().hex[:12]
        await self._db.execute(
            """INSERT OR REPLACE INTO opportunities
               (id, event_title, team_a, team_b, platform_buy_yes, platform_buy_no,
                yes_price, no_price, total_cost, profit_pct, roi_after_fees,
                found_at, still_active, details, sport)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                opp.id, opp.event_title, opp.team_a, opp.team_b,
                opp.platform_buy_yes.value, opp.platform_buy_no.value,
                opp.yes_price, opp.no_price, opp.total_cost,
                opp.profit_pct, opp.roi_after_fees,
                opp.found_at.isoformat(), int(opp.still_active),
                json.dumps(opp.details), sport,
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

    async def get_analytics(self) -> dict:
        """Aggregate analytics from the opportunities table."""
        result: dict = {}

        # Total count
        cur = await self._db.execute("SELECT COUNT(*) FROM opportunities")
        result["total_arbs_found"] = (await cur.fetchone())[0]

        # Active count
        cur = await self._db.execute("SELECT COUNT(*) FROM opportunities WHERE still_active = 1")
        result["active_arbs"] = (await cur.fetchone())[0]

        # By sport
        cur = await self._db.execute(
            "SELECT COALESCE(NULLIF(sport, ''), 'unknown') as s, COUNT(*) as c FROM opportunities GROUP BY s ORDER BY c DESC"
        )
        result["by_sport"] = {row[0]: row[1] for row in await cur.fetchall()}

        # By confidence (from details JSON)
        by_confidence = {"high": 0, "medium": 0, "low": 0}
        cur = await self._db.execute("SELECT details FROM opportunities")
        for row in await cur.fetchall():
            try:
                d = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                conf = d.get("confidence", "low") if isinstance(d, dict) else "low"
            except (json.JSONDecodeError, TypeError):
                conf = "low"
            by_confidence[conf] = by_confidence.get(conf, 0) + 1
        result["by_confidence"] = by_confidence

        # Average and min/max ROI
        cur = await self._db.execute(
            "SELECT AVG(roi_after_fees), MIN(roi_after_fees), MAX(roi_after_fees) FROM opportunities"
        )
        row = await cur.fetchone()
        result["avg_roi"] = round(row[0] or 0, 2)
        result["min_roi"] = round(row[1] or 0, 2)
        result["max_roi"] = round(row[2] or 0, 2)

        # Suspicious count
        cur = await self._db.execute(
            "SELECT COUNT(*) FROM opportunities WHERE details LIKE '%\"suspicious\": true%' OR details LIKE '%\"suspicious\":true%'"
        )
        result["suspicious_count"] = (await cur.fetchone())[0]

        # Last 24h and 7d counts
        cur = await self._db.execute(
            "SELECT COUNT(*) FROM opportunities WHERE found_at >= datetime('now', '-1 day')"
        )
        result["arbs_last_24h"] = (await cur.fetchone())[0]

        cur = await self._db.execute(
            "SELECT COUNT(*) FROM opportunities WHERE found_at >= datetime('now', '-7 days')"
        )
        result["arbs_last_7d"] = (await cur.fetchone())[0]

        # ROI distribution buckets
        roi_dist = {"0-2%": 0, "2-5%": 0, "5-10%": 0, "10-20%": 0, "20-50%": 0, "50%+": 0}
        cur = await self._db.execute("SELECT roi_after_fees FROM opportunities")
        for row in await cur.fetchall():
            roi = row[0] or 0
            if roi < 2:
                roi_dist["0-2%"] += 1
            elif roi < 5:
                roi_dist["2-5%"] += 1
            elif roi < 10:
                roi_dist["5-10%"] += 1
            elif roi < 20:
                roi_dist["10-20%"] += 1
            elif roi < 50:
                roi_dist["20-50%"] += 1
            else:
                roi_dist["50%+"] += 1
        result["roi_distribution"] = roi_dist

        # Daily arbs for last 7 days
        cur = await self._db.execute(
            """SELECT date(found_at) as d, COUNT(*) as c
               FROM opportunities
               WHERE found_at >= datetime('now', '-7 days')
               GROUP BY d ORDER BY d"""
        )
        result["daily_arbs"] = {row[0]: row[1] for row in await cur.fetchall()}

        # Recent 20 opportunities
        cur = await self._db.execute(
            "SELECT * FROM opportunities ORDER BY found_at DESC LIMIT 20"
        )
        result["recent"] = [dict(r) for r in await cur.fetchall()]

        return result

    async def cleanup_old(self, days: int = 7) -> int:
        """Delete inactive opportunities older than `days` days."""
        cursor = await self._db.execute(
            "DELETE FROM opportunities WHERE still_active = 0 AND found_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        return cursor.rowcount


db = Database()

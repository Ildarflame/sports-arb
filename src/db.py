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

CREATE TABLE IF NOT EXISTS roi_snapshots (
    opp_id TEXT NOT NULL,
    roi REAL NOT NULL,
    snapped_at TEXT NOT NULL,
    FOREIGN KEY (opp_id) REFERENCES opportunities(id)
);

CREATE INDEX IF NOT EXISTS idx_roi_snap ON roi_snapshots(opp_id, snapped_at);
"""

# Migration: add sport column if missing (for existing databases)
_MIGRATION_ADD_SPORT = "ALTER TABLE opportunities ADD COLUMN sport TEXT DEFAULT ''"

# Migration: add lifetime tracking columns
_MIGRATION_ADD_LIFETIME = [
    "ALTER TABLE opportunities ADD COLUMN first_seen TEXT",
    "ALTER TABLE opportunities ADD COLUMN last_seen TEXT",
    "ALTER TABLE opportunities ADD COLUMN deactivated_at TEXT",
]


class Database:
    def __init__(self, db_path: str = ""):
        self.db_path = db_path or settings.db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        # Run migrations for existing databases
        for migration in [_MIGRATION_ADD_SPORT] + _MIGRATION_ADD_LIFETIME:
            try:
                await self._db.execute(migration)
            except Exception:
                pass  # Column already exists
        # One-time cleanup: purge legacy garbage data (ROI > 50% = stale/illiquid artifacts)
        cur = await self._db.execute(
            "DELETE FROM opportunities WHERE roi_after_fees > 50 AND still_active = 0"
        )
        if cur.rowcount:
            import logging
            logging.getLogger(__name__).info(
                f"DB cleanup: purged {cur.rowcount} legacy garbage rows (ROI > 50%%)"
            )
        # Backfill sport column for rows that have sport=''
        await self._backfill_sport()
        await self._db.commit()

    async def _backfill_sport(self) -> None:
        """Backfill empty sport column from event_title patterns."""
        import logging
        _log = logging.getLogger(__name__)
        cursor = await self._db.execute(
            "SELECT id, event_title, team_a, team_b FROM opportunities WHERE sport = '' OR sport IS NULL"
        )
        rows = await cursor.fetchall()
        if not rows:
            return

        # Simple sport detection from team/title patterns
        _sport_hints = [
            (["nba", "lakers", "celtics", "warriors", "nets", "knicks", "bucks", "76ers",
              "cavaliers", "thunder", "nuggets", "timberwolves", "pelicans", "rockets",
              "spurs", "mavericks", "clippers", "suns", "grizzlies", "hawks", "heat",
              "magic", "pacers", "pistons", "raptors", "hornets", "wizards", "jazz", "kings",
              "blazers", "trail blazers"], "nba"),
            (["nhl", "bruins", "penguins", "maple leafs", "canadiens", "rangers", "blackhawks",
              "red wings", "flyers", "capitals", "oilers", "avalanche", "lightning", "panthers",
              "hurricanes", "flames", "canucks", "senators", "blue jackets", "predators",
              "kraken", "wild", "islanders", "sabres", "sharks", "ducks", "coyotes",
              "jets", "devils", "stars"], "nhl"),
            (["premier league", "epl", "la liga", "bundesliga", "serie a", "ligue 1",
              "champions league", "mls", "fc ", " fc", "united", "city", "real madrid",
              "barcelona", "liverpool", "arsenal", "chelsea", "tottenham", "juventus",
              "bayern", "dortmund", "psg", "inter milan", "ac milan", "napoli",
              "atletico", "sevilla"], "soccer"),
            (["ncaa", "wildcats", "bulldogs", "tigers", "eagles", "bears", "aggies",
              "huskies", "mustangs", "cardinals", "gators", "seminoles", "cyclones",
              "mountaineers", "longhorns", "sooners", "wolverines", "buckeyes",
              "crimson tide", "jayhawks", "duke", "unc", "gonzaga", "kentucky",
              "villanova", "kansas", "purdue", "iowa", "indiana", "michigan",
              "ohio state", "michigan st", "michigan state"], "ncaa_mb"),
            (["atp", "wta", "open", "stefanini", "djokovic", "nadal", "federer",
              "sinner", "alcaraz", "swiatek", "sabalenka", "gauff"], "tennis"),
            (["ufc", "mma"], "mma"),
        ]

        updated = 0
        for row in rows:
            title_lower = (row["event_title"] or "").lower()
            team_lower = ((row["team_a"] or "") + " " + (row["team_b"] or "")).lower()
            search_text = title_lower + " " + team_lower
            sport = ""
            for keywords, sport_name in _sport_hints:
                for kw in keywords:
                    if kw in search_text:
                        sport = sport_name
                        break
                if sport:
                    break
            if sport:
                await self._db.execute(
                    "UPDATE opportunities SET sport = ? WHERE id = ?",
                    (sport, row["id"]),
                )
                updated += 1

        if updated:
            _log.info(f"DB backfill: updated sport for {updated}/{len(rows)} opportunities")

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
        now_iso = datetime.utcnow().isoformat()
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
                       found_at = ?, details = ?, sport = ?,
                       last_seen = ?
                   WHERE id = ?""",
                (
                    opp.yes_price, opp.no_price, opp.total_cost,
                    opp.profit_pct, opp.roi_after_fees,
                    opp.found_at.isoformat(), json.dumps(opp.details),
                    sport, now_iso, opp.id,
                ),
            )
            return opp.id

        if not opp.id:
            opp.id = uuid.uuid4().hex[:12]
        await self._db.execute(
            """INSERT OR REPLACE INTO opportunities
               (id, event_title, team_a, team_b, platform_buy_yes, platform_buy_no,
                yes_price, no_price, total_cost, profit_pct, roi_after_fees,
                found_at, still_active, details, sport,
                first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                opp.id, opp.event_title, opp.team_a, opp.team_b,
                opp.platform_buy_yes.value, opp.platform_buy_no.value,
                opp.yes_price, opp.no_price, opp.total_cost,
                opp.profit_pct, opp.roi_after_fees,
                opp.found_at.isoformat(), int(opp.still_active),
                json.dumps(opp.details), sport,
                now_iso, now_iso,
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
        now_iso = datetime.utcnow().isoformat()
        await self._db.execute(
            "UPDATE opportunities SET still_active = 0, deactivated_at = ? WHERE id = ?",
            (now_iso, opp_id),
        )

    async def deactivate_by_key(
        self, team_a: str, platform_yes: str, platform_no: str,
    ) -> int:
        """Deactivate ALL active opportunities matching the given key."""
        now_iso = datetime.utcnow().isoformat()
        cursor = await self._db.execute(
            """UPDATE opportunities SET still_active = 0, deactivated_at = ?
               WHERE still_active = 1
                 AND team_a = ? AND platform_buy_yes = ? AND platform_buy_no = ?""",
            (now_iso, team_a, platform_yes, platform_no),
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

        # Lifetime stats (for opportunities that have been deactivated with lifetime tracking)
        cur = await self._db.execute(
            """SELECT AVG(julianday(deactivated_at) - julianday(first_seen)) * 24 * 60
               FROM opportunities
               WHERE deactivated_at IS NOT NULL AND first_seen IS NOT NULL"""
        )
        row = await cur.fetchone()
        result["avg_lifetime_min"] = round(row[0] or 0, 1)

        # Lifetime distribution
        lifetime_dist = {"< 1min": 0, "1-5min": 0, "5-30min": 0, "30min+": 0}
        cur = await self._db.execute(
            """SELECT (julianday(deactivated_at) - julianday(first_seen)) * 24 * 60 as mins
               FROM opportunities
               WHERE deactivated_at IS NOT NULL AND first_seen IS NOT NULL"""
        )
        for row in await cur.fetchall():
            mins = row[0] or 0
            if mins < 1:
                lifetime_dist["< 1min"] += 1
            elif mins < 5:
                lifetime_dist["1-5min"] += 1
            elif mins < 30:
                lifetime_dist["5-30min"] += 1
            else:
                lifetime_dist["30min+"] += 1
        result["lifetime_distribution"] = lifetime_dist

        # Recent 20 opportunities
        cur = await self._db.execute(
            "SELECT * FROM opportunities ORDER BY found_at DESC LIMIT 20"
        )
        result["recent"] = [dict(r) for r in await cur.fetchall()]

        return result

    async def save_roi_snapshot(self, opp_id: str, roi: float) -> None:
        """Record a ROI snapshot for an active opportunity."""
        now_iso = datetime.utcnow().isoformat()
        await self._db.execute(
            "INSERT INTO roi_snapshots (opp_id, roi, snapped_at) VALUES (?, ?, ?)",
            (opp_id, roi, now_iso),
        )

    async def get_roi_history(self, opp_id: str) -> list[dict]:
        """Return ROI time series for an opportunity."""
        cursor = await self._db.execute(
            "SELECT roi, snapped_at FROM roi_snapshots WHERE opp_id = ? ORDER BY snapped_at",
            (opp_id,),
        )
        return [{"roi": r[0], "time": r[1]} for r in await cursor.fetchall()]

    async def get_historical_opps(self, days: int = 30) -> list[dict]:
        """Return all opportunities with lifetime data for simulation."""
        cursor = await self._db.execute(
            """SELECT * FROM opportunities
               WHERE found_at >= datetime('now', ?)
               ORDER BY found_at""",
            (f"-{days} days",),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def simulate_pnl(
        self,
        bankroll: float = 1000.0,
        min_roi: float = 1.0,
        min_confidence: str = "low",
        days: int = 30,
    ) -> dict:
        """Simulate P&L from historical opportunities."""
        opps = await self.get_historical_opps(days)

        _conf_order = {"high": 0, "medium": 1, "low": 2}
        min_conf_level = _conf_order.get(min_confidence, 2)

        total_bets = 0
        total_profit = 0.0
        by_sport: dict[str, float] = {}
        by_day: dict[str, float] = {}
        best_day_profit = 0.0
        worst_day_profit = 0.0
        lifetimes: list[float] = []

        for opp in opps:
            roi = opp.get("roi_after_fees", 0) or 0
            if roi < min_roi:
                continue

            details = opp.get("details", "{}")
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except (json.JSONDecodeError, TypeError):
                    details = {}
            conf = details.get("confidence", "low")
            if _conf_order.get(conf, 2) > min_conf_level:
                continue

            # Calculate profit for this bet
            total_cost = opp.get("total_cost", 0) or 0
            if total_cost <= 0 or total_cost >= 1:
                continue

            units = bankroll / total_cost
            profit = (1.0 - total_cost) * units
            total_profit += profit
            total_bets += 1

            sport = opp.get("sport", "unknown") or "unknown"
            by_sport[sport] = by_sport.get(sport, 0) + profit

            found = opp.get("found_at", "")
            day = found[:10] if found else "unknown"
            by_day[day] = by_day.get(day, 0) + profit

            # Lifetime tracking
            first = opp.get("first_seen")
            deact = opp.get("deactivated_at")
            if first and deact:
                try:
                    from datetime import datetime as _dt
                    f_dt = _dt.fromisoformat(first)
                    d_dt = _dt.fromisoformat(deact)
                    lifetimes.append((d_dt - f_dt).total_seconds() / 60)
                except (ValueError, TypeError):
                    pass

        avg_hold = sum(lifetimes) / len(lifetimes) if lifetimes else 0
        for day, profit in by_day.items():
            if profit > best_day_profit:
                best_day_profit = profit
            if profit < worst_day_profit:
                worst_day_profit = profit

        return {
            "total_bets": total_bets,
            "total_profit": round(total_profit, 2),
            "roi_on_capital": round(total_profit / bankroll * 100, 2) if bankroll > 0 else 0,
            "avg_hold_min": round(avg_hold, 1),
            "by_sport": {k: round(v, 2) for k, v in sorted(by_sport.items(), key=lambda x: -x[1])},
            "by_day": by_day,
            "best_day": round(best_day_profit, 2),
            "worst_day": round(worst_day_profit, 2),
            "bankroll": bankroll,
            "min_roi": min_roi,
            "min_confidence": min_confidence,
            "days": days,
        }

    async def cleanup_old_snapshots(self, days: int = 7) -> int:
        """Delete ROI snapshots for deactivated arbs older than `days`."""
        cursor = await self._db.execute(
            """DELETE FROM roi_snapshots WHERE opp_id IN (
                SELECT id FROM opportunities
                WHERE still_active = 0 AND deactivated_at < datetime('now', ?)
            )""",
            (f"-{days} days",),
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

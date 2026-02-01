from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime

import httpx

from src.config import settings
from src.connectors.base import BaseConnector
from src.models import Market, MarketPrice, Platform

logger = logging.getLogger(__name__)

# Map Kalshi series/event ticker prefixes to sport codes
_KALSHI_SPORT_MAP: dict[str, str] = {
    "KXEPL": "soccer", "KXLALIGA": "soccer", "KXBUNDESLIGA": "soccer",
    "KXSERIEA": "soccer", "KXLIGUE1": "soccer", "KXSAUDIPL": "soccer",
    "KXEKSTRAKLASA": "soccer", "KXSUPERLIG": "soccer", "KXBELGIANPL": "soccer",
    "KXUCL": "soccer", "KXMENWORLDCUP": "soccer",
    "KXNBA": "nba", "KXNCAAMB": "ncaa_mb", "KXNCAAWB": "ncaa_wb",
    "KXNFL": "nfl", "KXNCAAF": "ncaa_fb", "KXHEISMAN": "ncaa_fb",
    "KXSUPERBOWL": "nfl",
    "KXNHL": "nhl",
    "KXMLB": "mlb",
    "KXUFC": "mma",
    "KXATP": "tennis", "KXWTA": "tennis",
    "KXFOPENMENSINGLE": "tennis", "KXWIMBLEDONMENSINGLE": "tennis",
    "KXAUSOPENMENSINGLE": "tennis", "KXUSOPENMENSINGLE": "tennis",
    "KXCS2": "esports", "KXLOL": "esports", "KXVALORANT": "esports", "KXDOTA2": "esports",
}


def _detect_sport_kalshi(event_ticker: str) -> str:
    """Detect sport from Kalshi event ticker prefix."""
    upper = event_ticker.upper()
    for prefix, sport in _KALSHI_SPORT_MAP.items():
        if upper.startswith(prefix):
            return sport
    return ""


def _parse_date_from_kalshi_ticker(event_ticker: str) -> date | None:
    """Parse date from Kalshi event ticker like KXNBAGAME-26FEB01-..."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-", event_ticker.upper())
    if m:
        year_2 = m.group(1)
        month_str = m.group(2)
        day = m.group(3)
        months = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
            "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
        }
        mon = months.get(month_str)
        if mon:
            try:
                return date(2000 + int(year_2), mon, int(day))
            except ValueError:
                pass
    return None


def _parse_date_from_iso(dt_str: str | None) -> date | None:
    """Parse date from ISO datetime string."""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _kalshi_event_group(series_ticker: str, event_ticker: str) -> str:
    """Derive event group identifier for futures matching."""
    # Use series_ticker if available (e.g. KXNFLMVP, KXNBACHAMP)
    if series_ticker:
        return series_ticker.upper()
    # Fallback: strip date/suffix from event_ticker
    # e.g. KXSUPERBOWL-26 -> KXSUPERBOWL
    m = re.match(r"^([A-Z]+?)(?:-\d)", event_ticker.upper())
    if m:
        return m.group(1)
    return event_ticker.upper()

# Event ticker prefixes for sports markets on Kalshi
GAME_TICKER_PREFIXES = (
    # Game/match markets (daily)
    "KXEPLGAME", "KXLALIGAGAME", "KXBUNDESLIGAGAME", "KXSERIEAGAME",
    "KXLIGUE1GAME", "KXSAUDIPLGAME", "KXEKSTRAKLASAGAME", "KXSUPERLIGGAME",
    "KXBELGIANPLGAME",  # Soccer
    "KXNBAGAME", "KXNCAAMBGAME", "KXNCAAWBGAME",  # Basketball
    "KXNFLGAME", "KXNCAAFGAME",  # Football
    "KXNHLGAME",  # Hockey
    "KXMLBGAME",  # Baseball
    "KXUFCGAME", "KXUFCMATCH",  # MMA
    "KXATPMATCH", "KXATPCHALLENGERMATCH", "KXWTAMATCH",  # Tennis
    "KXCS2GAME", "KXLOLGAME", "KXVALORANTGAME", "KXDOTA2GAME",  # Esports
    # Championship/futures markets (matchable with Polymarket)
    "KXNCAAF-", "KXNCAAFFINALIST",  # College football championship
    "KXHEISMAN",  # Heisman
    "KXSUPERBOWL",  # Super Bowl
)


class KalshiConnector(BaseConnector):
    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._token: str = ""

    async def connect(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.kalshi_api_base,
            timeout=30,
        )
        if settings.kalshi_email and settings.kalshi_password:
            await self._authenticate()
        else:
            logger.warning("Kalshi credentials not set — running without auth")

    async def _authenticate(self) -> None:
        try:
            resp = await self._http.post(
                "/log-in",
                json={
                    "email": settings.kalshi_email,
                    "password": settings.kalshi_password,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data.get("token", "")
            if self._token:
                self._http.headers["Authorization"] = f"Bearer {self._token}"
                logger.info("Kalshi: authenticated successfully")
        except Exception:
            logger.exception("Kalshi authentication failed")

    async def disconnect(self) -> None:
        if self._http:
            await self._http.aclose()

    # Known championship/futures event tickers to fetch directly
    FUTURES_EVENT_TICKERS = (
        "KXNCAAF-27", "KXNCAAFFINALIST-27",  # College football
        "KXSUPERBOWL-26",  # Super Bowl
        "KXHEISMAN-26",  # Heisman trophy
    )

    # Championship/futures series tickers on Kalshi (matchable with Polymarket futures)
    FUTURES_SERIES_TICKERS = (
        # NBA
        "KXNBACHAMP", "KXNBAEAST", "KXNBAWEST", "KXNBAMVP", "KXNBAFINMVP",
        "KXNBADROTY", "KXNBADPOY",
        # NFL
        "KXNFLCHAMP", "KXNFLMVP", "KXNFLDROTY", "KXNFLSBMVP",
        # MLB
        "KXMLBWS", "KXMLBALCHAMP", "KXMLBNLCHAMP", "KXMLBMVP",
        # NHL
        "KXNHLCHAMP", "KXNHLFINALSEXACT",
        # Soccer
        "KXUCLCHAMP", "KXEPLCHAMP", "KXEPLTOP4", "KXLALIGACHAMP",
        "KXBUNDESLIGACHAMP", "KXSERIEACHAMP", "KXLIGUE1CHAMP",
        "KXMENWORLDCUP",
        # College
        "KXNCAAFBCHAMP", "KXNCAAMBCHAMP",
        # Tennis Grand Slams
        "KXFOPENMENSINGLE", "KXWIMBLEDONMENSINGLE",
        "KXAUSOPENMENSINGLE", "KXUSOPENMENSINGLE",
        # MMA
        "KXUFCCHAMP",
    )

    # Daily game series tickers (fetched via /events endpoint)
    GAME_SERIES_TICKERS = (
        # Soccer
        "KXEPLGAME", "KXLALIGAGAME", "KXBUNDESLIGAGAME", "KXSERIEAGAME",
        "KXLIGUE1GAME", "KXSAUDIPLGAME", "KXEKSTRAKLASAGAME", "KXSUPERLIGGAME",
        "KXBELGIANPLGAME",
        # Basketball
        "KXNBAGAME", "KXNCAAMBGAME", "KXNCAAWBGAME",
        # Football
        "KXNFLGAME", "KXNCAAFGAME",
        # Hockey
        "KXNHLGAME",
        # Baseball
        "KXMLBGAME",
        # MMA
        "KXUFCGAME", "KXUFCMATCH",
        # Tennis
        "KXATPMATCH", "KXATPCHALLENGERMATCH", "KXWTAMATCH",
        # Esports
        "KXCS2GAME", "KXLOLGAME", "KXVALORANTGAME", "KXDOTA2GAME",
    )

    async def _fetch_markets_for_event(self, event_ticker: str) -> list[dict]:
        """Fetch all markets for a specific event ticker."""
        try:
            resp = await self._http.get(
                "/markets",
                params={"event_ticker": event_ticker, "status": "open", "limit": 200},
            )
            resp.raise_for_status()
            return resp.json().get("markets", [])
        except Exception:
            logger.warning(f"Failed to fetch markets for {event_ticker}")
            return []

    async def fetch_sports_events(self) -> list[Market]:
        """Fetch sports markets from Kalshi.

        Two strategies:
        1. Scan /markets paginated for game tickers (daily matches)
        2. Fetch specific known championship event tickers directly
        """
        markets: list[Market] = []
        seen_tickers: set[str] = set()
        cursor = ""
        pages = 0

        try:
            # Strategy 1: Paginate /markets for daily game markets
            while pages < 30:
                params: dict = {
                    "status": "open",
                    "limit": 200,
                }
                if cursor:
                    params["cursor"] = cursor

                resp = await self._http.get("/markets", params=params)
                resp.raise_for_status()
                data = resp.json()

                for m in data.get("markets", []):
                    event_ticker = m.get("event_ticker", "")

                    # Filter: only game/match markets
                    if not any(event_ticker.startswith(prefix) for prefix in GAME_TICKER_PREFIXES):
                        continue

                    title = m.get("title", "")
                    ticker = m.get("ticker", "")

                    # Extract team names from title
                    # If title is a question ("Will X win..."), use question parser first
                    # to avoid _parse_teams splitting on "vs" inside match descriptions
                    if title.lower().startswith("will "):
                        team_a = self._extract_team_from_question(title)
                        team_b = ""
                        if not team_a:
                            team_a, team_b = self._parse_teams(title)
                    else:
                        team_a, team_b = self._parse_teams(title)
                        if not team_a:
                            team_a = self._extract_team_from_question(title)
                            team_b = ""
                    if not team_a:
                        logger.debug(f"Kalshi: skipping market (no team): {title[:80]}")
                        continue

                    # Determine which team this specific market is for
                    # Kalshi has separate markets per outcome (TOT, MCI, TIE)
                    # The ticker suffix indicates the team
                    no_sub = m.get("no_sub_title", "")
                    rules = m.get("rules_primary", "")

                    # Skip "Tie" markets — we only want team-win markets
                    if ticker.endswith("-TIE") or no_sub.lower() == "tie":
                        continue

                    # Figure out which team this YES represents
                    # from rules: "If [Team] wins the..."
                    yes_team = self._extract_team_from_rules(rules)
                    if not yes_team:
                        yes_team = no_sub  # no_sub_title is sometimes the YES team name

                    market = Market(
                        platform=Platform.KALSHI,
                        market_id=ticker,
                        event_id=event_ticker,
                        title=title,
                        team_a=team_a,
                        team_b=team_b,
                        category="sports",
                        market_type="game",  # Strategy 1 only fetches game markets
                        sport=_detect_sport_kalshi(event_ticker),
                        game_date=(
                            _parse_date_from_kalshi_ticker(event_ticker)
                            or _parse_date_from_iso(m.get("close_time") or m.get("expiration_time"))
                        ),
                        url=f"https://kalshi.com/events/{event_ticker.lower()}",
                        raw_data={
                            "yes_team": yes_team,
                            "no_sub_title": no_sub,
                            "event_ticker": event_ticker,
                            "series_ticker": m.get("series_ticker", ""),
                        },
                    )

                    # Set initial price from bid/ask
                    yes_bid = m.get("yes_bid")
                    yes_ask = m.get("yes_ask")
                    last_price = m.get("last_price")
                    if yes_bid is not None or yes_ask is not None:
                        yb = (yes_bid or 0) / 100
                        ya = (yes_ask or 0) / 100
                        mid = (yb + ya) / 2 if yb and ya else yb or ya
                        if last_price and not mid:
                            mid = last_price / 100
                        market.price = MarketPrice(
                            yes_price=round(mid, 4),
                            no_price=round(1 - mid, 4),
                            yes_bid=yb or None,
                            yes_ask=ya or None,
                            volume=m.get("volume", 0),
                            last_updated=datetime.now(UTC),
                        )

                    seen_tickers.add(ticker)
                    markets.append(market)

                cursor = data.get("cursor", "")
                pages += 1
                if not cursor or not data.get("markets"):
                    break

            game_count = len(markets)
            logger.info(f"Kalshi: {game_count} game markets from pagination ({pages} pages)")

            # Strategy 2: Fetch specific championship/futures events directly
            for evt_ticker in self.FUTURES_EVENT_TICKERS:
                evt_markets = await self._fetch_markets_for_event(evt_ticker)
                for m in evt_markets:
                    ticker = m.get("ticker", "")
                    if ticker in seen_tickers:
                        continue
                    parsed = self._parse_market(m)
                    if parsed:
                        seen_tickers.add(ticker)
                        markets.append(parsed)

            futures_from_hardcoded = len(markets) - game_count

            # Strategy 3: Fetch championship/futures events from known series
            for series_ticker in self.FUTURES_SERIES_TICKERS:
                try:
                    evt_cursor = ""
                    for _ in range(5):  # Max 5 pages per series
                        params = {
                            "status": "open",
                            "series_ticker": series_ticker,
                            "with_nested_markets": "true",
                            "limit": 50,
                        }
                        if evt_cursor:
                            params["cursor"] = evt_cursor
                        resp = await self._http.get("/events", params=params)
                        resp.raise_for_status()
                        events_data = resp.json()
                        for evt in events_data.get("events", []):
                            for m in evt.get("markets", []):
                                ticker = m.get("ticker", "")
                                if ticker in seen_tickers:
                                    continue
                                parsed = self._parse_market(m)
                                if parsed:
                                    seen_tickers.add(ticker)
                                    markets.append(parsed)
                        evt_cursor = events_data.get("cursor", "")
                        if not evt_cursor or not events_data.get("events"):
                            break
                except Exception:
                    logger.debug(f"Kalshi events search for series {series_ticker} failed")

            series_futures_count = len(markets) - game_count - futures_from_hardcoded

            # Strategy 4: Fetch daily game events from known game series
            for series_ticker in self.GAME_SERIES_TICKERS:
                try:
                    evt_cursor = ""
                    for _ in range(3):  # Max 3 pages per game series
                        params = {
                            "status": "open",
                            "series_ticker": series_ticker,
                            "with_nested_markets": "true",
                            "limit": 50,
                        }
                        if evt_cursor:
                            params["cursor"] = evt_cursor
                        resp = await self._http.get("/events", params=params)
                        resp.raise_for_status()
                        events_data = resp.json()
                        for evt in events_data.get("events", []):
                            for m in evt.get("markets", []):
                                ticker = m.get("ticker", "")
                                if ticker in seen_tickers:
                                    continue
                                parsed = self._parse_market(m, market_type="game")
                                if parsed:
                                    seen_tickers.add(ticker)
                                    markets.append(parsed)
                        evt_cursor = events_data.get("cursor", "")
                        if not evt_cursor or not events_data.get("events"):
                            break
                except Exception:
                    logger.debug(f"Kalshi game series {series_ticker} fetch failed")

            game_series_count = len(markets) - game_count - futures_from_hardcoded - series_futures_count

            logger.info(
                f"Kalshi: {len(markets)} total sports markets "
                f"({game_count} games + {futures_from_hardcoded} event futures "
                f"+ {series_futures_count} series futures + {game_series_count} game series)"
            )
        except Exception:
            logger.exception("Error fetching Kalshi sports markets")
        return markets

    def _parse_market(self, m: dict, market_type: str = "futures") -> Market | None:
        """Parse a raw Kalshi market dict into a Market object."""
        title = m.get("title", "")
        ticker = m.get("ticker", "")
        event_ticker = m.get("event_ticker", "")
        series_ticker = m.get("series_ticker", "")

        if title.lower().startswith("will "):
            team_a = self._extract_team_from_question(title)
            team_b = ""
            if not team_a:
                team_a, team_b = self._parse_teams(title)
        else:
            team_a, team_b = self._parse_teams(title)
            if not team_a:
                team_a = self._extract_team_from_question(title)
                team_b = ""
        if not team_a:
            return None

        no_sub = m.get("no_sub_title", "")
        rules = m.get("rules_primary", "")

        if ticker.endswith("-TIE") or no_sub.lower() == "tie":
            return None

        yes_team = self._extract_team_from_rules(rules)
        if not yes_team:
            yes_team = no_sub

        sport = _detect_sport_kalshi(event_ticker)
        game_date = None
        event_group = ""
        if market_type == "game":
            game_date = (
                _parse_date_from_kalshi_ticker(event_ticker)
                or _parse_date_from_iso(m.get("close_time") or m.get("expiration_time"))
            )
        else:
            event_group = _kalshi_event_group(series_ticker, event_ticker)

        market = Market(
            platform=Platform.KALSHI,
            market_id=ticker,
            event_id=event_ticker,
            title=title,
            team_a=team_a,
            team_b=team_b,
            category="sports",
            market_type=market_type,
            sport=sport,
            game_date=game_date,
            event_group=event_group,
            url=f"https://kalshi.com/events/{event_ticker.lower()}",
            raw_data={
                "yes_team": yes_team,
                "no_sub_title": no_sub,
                "event_ticker": event_ticker,
                "series_ticker": series_ticker,
            },
        )

        yes_bid = m.get("yes_bid")
        yes_ask = m.get("yes_ask")
        last_price = m.get("last_price")
        if yes_bid is not None or yes_ask is not None:
            yb = (yes_bid or 0) / 100
            ya = (yes_ask or 0) / 100
            mid = (yb + ya) / 2 if yb and ya else yb or ya
            if last_price and not mid:
                mid = last_price / 100
            market.price = MarketPrice(
                yes_price=round(mid, 4),
                no_price=round(1 - mid, 4),
                yes_bid=yb or None,
                yes_ask=ya or None,
                volume=m.get("volume", 0),
                last_updated=datetime.now(UTC),
            )

        return market

    async def fetch_price(self, market_id: str) -> MarketPrice | None:
        """Fetch price for a single Kalshi market."""
        try:
            resp = await self._http.get(f"/markets/{market_id}")
            resp.raise_for_status()
            data = resp.json().get("market", resp.json())

            yes_bid = (data.get("yes_bid") or 0) / 100
            yes_ask = (data.get("yes_ask") or 0) / 100
            no_bid = (data.get("no_bid") or 0) / 100
            no_ask = (data.get("no_ask") or 0) / 100

            last_price = data.get("last_price", 0)
            if last_price:
                yes_price = last_price / 100
            elif yes_bid and yes_ask:
                yes_price = (yes_bid + yes_ask) / 2
            else:
                yes_price = yes_bid or yes_ask

            return MarketPrice(
                yes_price=round(yes_price, 4),
                no_price=round(1 - yes_price, 4),
                yes_bid=yes_bid or None,
                yes_ask=yes_ask or None,
                no_bid=no_bid or None,
                no_ask=no_ask or None,
                volume=data.get("volume", 0),
                last_updated=datetime.now(UTC),
            )
        except Exception:
            logger.exception(f"Error fetching Kalshi price for {market_id}")
            return None

    @staticmethod
    def _parse_teams(title: str) -> tuple[str, str]:
        """Extract two team names from 'Team A vs Team B Winner?' style title."""
        for sep in (" vs. ", " vs ", " v. ", " v "):
            if sep in title:
                idx = title.index(sep)
                a = title[:idx].strip()
                b = title[idx + len(sep):].strip()
                # Remove trailing "Winner?", "Game?", etc.
                for suffix in ("Winner?", "Game?", "Match?", "?", " Winner", " Game", " Match"):
                    b = b.removesuffix(suffix).strip()
                    a = a.removesuffix(suffix).strip()
                # Remove leading "Will"
                for prefix in ("Will ",):
                    if a.startswith(prefix):
                        a = a[len(prefix):]
                # Remove colon-separated metadata (e.g., ": Qualification Round 1")
                if " : " in b:
                    b = b[:b.index(" : ")].strip()
                if " : " in a:
                    a = a[:a.index(" : ")].strip()
                return a.strip(), b.strip()
        return "", ""

    @staticmethod
    def _extract_team_from_question(question: str) -> str:
        """Extract team name from 'Will X win the Y?' pattern (futures)."""
        m = re.match(
            r"^Will\s+(?:the\s+)?(.+?)\s+win\s+(?:the\s+)?(?:\d{4}|College|NFL|NBA|NHL|MLB|Super|Stanley|FIFA|Champions|Premier|World)",
            question, re.IGNORECASE,
        )
        if m:
            return m.group(1).strip()
        m = re.match(r"^Will\s+(?:the\s+)?(.+?)\s+win\b", question, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            if len(name) > 2:
                return name
        return ""

    @staticmethod
    def _extract_team_from_rules(rules: str) -> str:
        """Extract the YES team from rules like 'If [Team] wins the...'"""
        m = re.match(r"^If\s+(.+?)\s+wins\s+the\b", rules, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return ""

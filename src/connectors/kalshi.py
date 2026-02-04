from __future__ import annotations

import asyncio
import base64
import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

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
    "KXCS2": "esports", "KXLOL": "esports", "KXVALORANT": "esports", "KXDOTA2": "esports",
    "KXCRICKET": "cricket", "KXIPL": "cricket", "KXICC": "cricket", "KXCRICKETT20IMATCH": "cricket",
    "KXPGATOUR": "golf", "KXLPGATOUR": "golf", "KXDPWORLDTOUR": "golf", "KXTGLMATCH": "golf",
    "KXSWISSLEAGUE": "soccer",
    "KXF1": "motorsport",
    "KXTABLETENNIS": "table_tennis",
}


def _detect_sport_kalshi(event_ticker: str) -> str:
    """Detect sport from Kalshi event ticker prefix."""
    upper = event_ticker.upper()
    for prefix, sport in _KALSHI_SPORT_MAP.items():
        if upper.startswith(prefix):
            return sport
    return ""


def _detect_market_subtype(ticker: str, title: str) -> tuple[str, float | None]:
    """Detect market subtype (moneyline/spread/over_under) and line value from Kalshi ticker/title.

    Kalshi ticker patterns:
      Spread: KXNBAGAME-26FEB01...-SP-3.5 or title contains "spread" / "+3.5" / "-3.5"
      O/U: KXNBAGAME-26FEB01...-OU-220.5 or title contains "over" / "under" / "total"
    """
    upper = ticker.upper()
    title_lower = title.lower()

    # Check ticker for spread indicator
    sp_match = re.search(r"-SP[+-]?(\d+\.?\d*)", upper)
    if sp_match:
        line = float(sp_match.group(1))
        # Determine sign: look at segment after "-SP" prefix
        sp_idx = upper.find("-SP") + 3  # skip past "-SP"
        sp_segment = upper[sp_idx:]
        if sp_segment.startswith("-"):
            return "spread", -line
        return "spread", line

    # Check ticker for O/U indicator
    ou_match = re.search(r"-(?:OU|TOTAL)(\d+\.?\d*)", upper)
    if ou_match:
        line = float(ou_match.group(1))
        # Sign convention: Over=positive, Under=negative
        # Check ticker for -OV or -UN prefix before the number
        if "-UN" in upper:
            return "over_under", -line
        # Also check title for "under"
        if "under" in title_lower and "over" not in title_lower:
            return "over_under", -line
        return "over_under", line

    # Fallback: check title
    if "spread" in title_lower or re.search(r"[+-]\d+\.5", title):
        m = re.search(r"([+-]\d+\.?\d*)", title)
        if m:
            return "spread", float(m.group(1))

    if any(kw in title_lower for kw in ("over ", "under ", "total ", "o/u ")):
        m = re.search(r"(?:over|under|total|o/u)\s+(\d+\.?\d*)", title_lower)
        if m:
            line = float(m.group(1))
            # Sign convention: under=negative
            if "under" in title_lower and "over" not in title_lower:
                return "over_under", -line
            return "over_under", line

    return "moneyline", None


def _detect_map_number(ticker: str, title: str) -> int | None:
    """Detect esports map number from Kalshi ticker or title.

    Patterns:
      Ticker: KXCS2GAME-26FEB03...-MAP1, KXDOTA2GAME-...-G2
      Title: "Map 1", "Map 2", "Game 1", "Game 2"
    """
    # Check ticker for map indicator (MAP1, MAP2, G1, G2)
    upper = ticker.upper()
    m = re.search(r"-(?:MAP|G)(\d+)", upper)
    if m:
        return int(m.group(1))

    # Check title
    m = re.search(r"(?:map|game)[\s\-]*(\d+)", title, re.IGNORECASE)
    if m:
        return int(m.group(1))

    return None


def _parse_date_from_kalshi_ticker(event_ticker: str) -> date | None:
    """Parse date from Kalshi event ticker like KXNBAGAME-26FEB01AVLBRE or KXNBAGAME-26FEB01-..."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker.upper())
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
    "KXUCLGAME",  # Champions League matches
    "KXNBAGAME", "KXNCAAMBGAME", "KXNCAAWBGAME",  # Basketball
    "KXNFLGAME", "KXNCAAFGAME",  # Football
    "KXNHLGAME",  # Hockey
    "KXMLBGAME",  # Baseball
    "KXUFCFIGHT",  # MMA
    "KXATPMATCH", "KXATPCHALLENGERMATCH", "KXWTAMATCH",  # Tennis
    "KXCS2GAME", "KXLOLGAME", "KXVALORANTGAME", "KXDOTA2GAME",  # Esports
    "KXTGLMATCH",  # Golf (TGL match play)
    "KXSWISSLEAGUEGAME",  # Soccer (Swiss Super League)
    "KXTABLETENNIS",  # Table Tennis
    "KXCRICKETT20IMATCH",  # Cricket T20I
    # Championship/futures markets (matchable with Polymarket)
    "KXNCAAF-", "KXNCAAFFINALIST",  # College football championship
    "KXHEISMAN",  # Heisman
    "KXSUPERBOWL",  # Super Bowl
)


class KalshiConnector(BaseConnector):
    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._private_key = None
        self._api_key_id: str = ""

    async def connect(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.kalshi_api_base,
            timeout=30,
        )
        if settings.kalshi_api_key_id and settings.kalshi_private_key_path:
            self._load_rsa_key()
        else:
            logger.warning("Kalshi RSA credentials not set — running without auth")

    def _load_rsa_key(self) -> None:
        """Load RSA private key for API authentication."""
        try:
            key_path = Path(settings.kalshi_private_key_path)
            if not key_path.is_absolute():
                # Resolve relative to project root
                key_path = Path(__file__).resolve().parent.parent.parent / key_path
            pem_data = key_path.read_bytes()
            self._private_key = serialization.load_pem_private_key(pem_data, password=None)
            self._api_key_id = settings.kalshi_api_key_id
            logger.info("Kalshi: RSA key loaded, authenticated requests enabled")
        except Exception:
            logger.exception("Kalshi: failed to load RSA key")

    def _sign_request(self, method: str, path: str) -> dict[str, str]:
        """Generate Kalshi auth headers with RSA-PSS signature."""
        if not self._private_key:
            return {}
        timestamp = str(int(datetime.now(UTC).timestamp() * 1000))
        # Strip query params for signing
        path_clean = path.split("?")[0]
        message = f"{timestamp}{method.upper()}{path_clean}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

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
        "KXNBA", "KXNBAEAST", "KXNBAWEST", "KXNBAMVP", "KXNBAFINMVP",
        "KXNBADPOY", "KXNBAROY", "KXNBACOY", "KXNBASIXTH", "KXNBAMIMP",
        "KXNBAPLAYOFF", "KXNBADRAFT1",
        # NFL
        "KXNFLMVP", "KXNFLDROTY", "KXNFLSBMVP",
        # MLB
        "KXMLB", "KXMLBAL", "KXMLBNL", "KXMLBPLAYOFFS",
        # NHL
        "KXNHL", "KXNHLEAST", "KXNHLWEST", "KXNHLPLAYOFF",
        # Soccer
        "KXUCL", "KXEPLTOP4", "KXEPLTOP2", "KXLALIGA",
        "KXBUNDESLIGA", "KXSERIEA", "KXLIGUE1",
        "KXMENWORLDCUP",
        # College
        "KXNCAAF",
        # Tennis
        "KXATPGRANDSLAM", "KXWTAGRANDSLAM",
        # Golf
        "KXPGATOUR", "KXLPGATOUR", "KXDPWORLDTOUR",
        # Motorsport
        "KXF1", "KXF1CONSTRUCTORS",
    )

    # Daily game series tickers (fetched via /events endpoint)
    GAME_SERIES_TICKERS = (
        # Soccer
        "KXEPLGAME", "KXLALIGAGAME", "KXBUNDESLIGAGAME", "KXSERIEAGAME",
        "KXLIGUE1GAME", "KXSAUDIPLGAME", "KXEKSTRAKLASAGAME", "KXSUPERLIGGAME",
        "KXBELGIANPLGAME", "KXUCLGAME",
        # Basketball
        "KXNBAGAME", "KXNCAAMBGAME", "KXNCAAWBGAME",
        # Football
        "KXNFLGAME", "KXNCAAFGAME",
        # Hockey
        "KXNHLGAME",
        # Baseball
        "KXMLBGAME",
        # MMA
        "KXUFCFIGHT",
        # Tennis
        "KXATPMATCH", "KXATPCHALLENGERMATCH", "KXWTAMATCH",
        "KXWTACHALLENGERMATCH",
        # Esports
        "KXCS2GAME", "KXLOLGAME", "KXVALORANTGAME", "KXDOTA2GAME",
        # Golf (TGL match play)
        "KXTGLMATCH",
        # Soccer (Swiss Super League)
        "KXSWISSLEAGUEGAME",
        # Table Tennis
        "KXTABLETENNIS",
        # Cricket T20I
        "KXCRICKETT20IMATCH",
    )

    async def _request_with_retry(
        self, method: str, path: str, *, params: dict | None = None, max_retries: int = 2,
    ) -> httpx.Response:
        """Make an HTTP request with retry on 429 (rate limit).

        Includes RSA-PSS auth headers if credentials are configured.
        """
        for attempt in range(max_retries + 1):
            # Build full path for signing (including query params)
            req = self._http.build_request(method, path, params=params)
            full_path = req.url.raw_path.decode()
            auth_headers = self._sign_request(method, full_path)

            # Debug logging for portfolio endpoints
            if "portfolio" in path:
                logger.info(f"Kalshi portfolio request: {method} {full_path}")

            resp = await self._http.request(method, path, params=params, headers=auth_headers)
            if resp.status_code == 429:
                wait = 2 * (attempt + 1)
                logger.warning(f"Kalshi 429 on {path}, retry {attempt+1}/{max_retries} after {wait}s")
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        # Final attempt got 429 — raise it
        resp.raise_for_status()
        return resp  # unreachable but keeps type checker happy

    async def _fetch_markets_for_event(self, event_ticker: str) -> list[dict]:
        """Fetch all markets for a specific event ticker."""
        try:
            resp = await self._request_with_retry(
                "GET", "/markets",
                params={"event_ticker": event_ticker, "status": "open", "limit": 200},
            )
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

                resp = await self._request_with_retry("GET", "/markets", params=params)
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

                    # Try to fill team_b from "X vs Y" in title if still empty
                    if not team_b:
                        _, extracted_b = self._extract_both_teams_from_title(title)
                        if extracted_b:
                            team_b = extracted_b

                    # Align team_a = YES team using yes_sub_title from API
                    yes_sub = m.get("yes_sub_title", "")
                    if team_b:
                        team_a, team_b = self._align_yes_team_v2(
                            team_a, team_b, yes_sub, ticker, event_ticker,
                        )

                    # Determine which team this specific market is for
                    # Kalshi has separate markets per outcome (TOT, MCI, TIE)
                    # The ticker suffix indicates the team
                    no_sub = m.get("no_sub_title", "")
                    rules = m.get("rules_primary", "")

                    # Handle Tie/Draw markets for 3-way arbitrage (soccer)
                    is_tie_market = ticker.endswith("-TIE") or no_sub.lower() == "tie"
                    if is_tie_market:
                        yes_team = "Draw"
                        s1_subtype = "draw"
                        s1_line = None  # Draw markets don't have lines
                    else:
                        # Figure out which team this YES represents
                        # from rules: "If [Team] wins the..."
                        yes_team = self._extract_team_from_rules(rules)
                        if not yes_team:
                            yes_team = no_sub  # no_sub_title is sometimes the YES team name

                        # Detect spread/OU subtype
                        s1_subtype, s1_line = _detect_market_subtype(ticker, title)

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
                        line=s1_line,
                        url=f"https://kalshi.com/markets/{(m.get('series_ticker') or event_ticker.split('-')[0]).lower()}/e/{event_ticker.lower()}",
                        raw_data={
                            "yes_team": yes_team,
                            "no_sub_title": no_sub,
                            "event_ticker": event_ticker,
                            "series_ticker": m.get("series_ticker", ""),
                            "market_subtype": s1_subtype,
                            "close_time": m.get("close_time"),
                            "expiration_time": m.get("expiration_time"),
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
                            no_bid=round(1 - ya, 4) if ya else None,
                            no_ask=round(1 - yb, 4) if yb else None,
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

            # Strategy 3: Fetch championship/futures events from known series (parallel)
            _series_sem = asyncio.Semaphore(5)

            async def _fetch_futures_series(series_ticker: str) -> list[Market]:
                async with _series_sem:
                    await asyncio.sleep(0.1)  # Small stagger between launches
                    result: list[Market] = []
                    try:
                        evt_cursor = ""
                        for _ in range(5):
                            params = {
                                "status": "open",
                                "series_ticker": series_ticker,
                                "with_nested_markets": "true",
                                "limit": 50,
                            }
                            if evt_cursor:
                                params["cursor"] = evt_cursor
                            resp = await self._request_with_retry("GET", "/events", params=params)
                            events_data = resp.json()
                            for evt in events_data.get("events", []):
                                for m in evt.get("markets", []):
                                    ticker = m.get("ticker", "")
                                    if ticker in seen_tickers:
                                        continue
                                    parsed = self._parse_market(m)
                                    if parsed:
                                        result.append(parsed)
                            evt_cursor = events_data.get("cursor", "")
                            if not evt_cursor or not events_data.get("events"):
                                break
                    except Exception:
                        logger.debug(f"Kalshi events search for series {series_ticker} failed")
                    return result

            futures_results = await asyncio.gather(
                *[_fetch_futures_series(st) for st in self.FUTURES_SERIES_TICKERS],
                return_exceptions=True,
            )
            for res in futures_results:
                if isinstance(res, list):
                    for m in res:
                        if m.market_id not in seen_tickers:
                            seen_tickers.add(m.market_id)
                            markets.append(m)

            series_futures_count = len(markets) - game_count - futures_from_hardcoded

            # Strategy 4: Fetch daily game events from known game series (parallel)
            async def _fetch_game_series(series_ticker: str) -> list[Market]:
                async with _series_sem:
                    await asyncio.sleep(0.1)
                    result: list[Market] = []
                    try:
                        evt_cursor = ""
                        for _ in range(3):
                            params = {
                                "status": "open",
                                "series_ticker": series_ticker,
                                "with_nested_markets": "true",
                                "limit": 50,
                            }
                            if evt_cursor:
                                params["cursor"] = evt_cursor
                            resp = await self._request_with_retry("GET", "/events", params=params)
                            events_data = resp.json()
                            for evt in events_data.get("events", []):
                                evt_parsed: list[Market] = []
                                for m in evt.get("markets", []):
                                    ticker = m.get("ticker", "")
                                    if ticker in seen_tickers:
                                        continue
                                    parsed = self._parse_market(m, market_type="game")
                                    if parsed:
                                        evt_parsed.append(parsed)
                                if len(evt_parsed) == 2:
                                    a, b = evt_parsed
                                    if a.team_a.lower() != b.team_a.lower():
                                        if not a.team_b or len(a.team_b.split()) == 1:
                                            a.team_b = b.team_a
                                        if not b.team_b or len(b.team_b.split()) == 1:
                                            b.team_b = a.team_a
                                result.extend(evt_parsed)
                            evt_cursor = events_data.get("cursor", "")
                            if not evt_cursor or not events_data.get("events"):
                                break
                    except Exception:
                        logger.debug(f"Kalshi game series {series_ticker} fetch failed")
                    return result

            game_results = await asyncio.gather(
                *[_fetch_game_series(st) for st in self.GAME_SERIES_TICKERS],
                return_exceptions=True,
            )
            for res in game_results:
                if isinstance(res, list):
                    for m in res:
                        if m.market_id not in seen_tickers:
                            seen_tickers.add(m.market_id)
                            markets.append(m)

            game_series_count = len(markets) - game_count - futures_from_hardcoded - series_futures_count

            # Deduplicate per-team markets: for each game event with 2 complementary
            # team markets (e.g. "Will TYLOO win?" + "Will DRG win?"), keep only one.
            # Both are just complements (YES_A = NO_B), so one market suffices.
            pre_dedup = len(markets)
            markets = self._dedup_per_team_markets(markets)
            dedup_removed = pre_dedup - len(markets)

            logger.info(
                f"Kalshi: {len(markets)} total sports markets "
                f"({game_count} games + {futures_from_hardcoded} event futures "
                f"+ {series_futures_count} series futures + {game_series_count} game series"
                f"{f' - {dedup_removed} deduped' if dedup_removed else ''})"
            )
        except Exception:
            logger.exception("Error fetching Kalshi sports markets")
        return markets

    @staticmethod
    def _dedup_per_team_markets(markets: list[Market]) -> list[Market]:
        """Deduplicate per-team game markets from the same Kalshi event.

        Kalshi creates separate YES/NO markets per team for each game event:
          - KXVALORANTGAME-26FEB03DRGTYLOO-DRG  ("Will DRG win?")
          - KXVALORANTGAME-26FEB03DRGTYLOO-TYLOO ("Will TYLOO win?")

        These are complements (YES on one = NO on the other). Keeping both
        causes the matcher to potentially pick the wrong team's market,
        leading to both arb legs betting on the same outcome.

        For each event with exactly 2 game markets, keep only the first
        market (by ticker order) and ensure team_b is populated.
        """
        from collections import defaultdict

        # Group game markets by event_id
        event_groups: dict[str, list[Market]] = defaultdict(list)
        non_game: list[Market] = []
        for m in markets:
            if m.market_type == "game" and m.event_id:
                event_groups[m.event_id].append(m)
            else:
                non_game.append(m)

        result = list(non_game)
        for event_id, group in event_groups.items():
            if len(group) == 2:
                # Two complementary per-team markets — keep the one whose ticker
                # suffix matches the FIRST team code in the event ticker.
                # E.g., event KXNHLGAME-26FEB02STLNSH → first team = STL
                # Keep market -STL so that team_a = YES team = first team.
                # This ensures _align_yes_team works correctly and avoids
                # mismatches from abbreviated yes_sub_title (e.g. "NSH Predators").
                group.sort(key=lambda x: x.market_id)
                keep, drop = group[0], group[1]

                # Try to identify first-team market from event ticker
                # Event ticker pattern: PREFIX-DATEABCXYZ where ABC and XYZ are team codes
                # Market ticker: PREFIX-DATEABCXYZ-ABC or PREFIX-DATEABCXYZ-XYZ
                for m in group:
                    suffix = m.market_id.rsplit("-", 1)[-1].upper()
                    # Check if this suffix appears FIRST in the event ticker
                    # by seeing if it comes before the other market's suffix
                    evt_upper = (m.event_id or "").upper()
                    if suffix and suffix in evt_upper:
                        other = [x for x in group if x is not m][0]
                        other_suffix = other.market_id.rsplit("-", 1)[-1].upper()
                        idx_this = evt_upper.rfind(suffix)
                        idx_other = evt_upper.rfind(other_suffix)
                        if idx_this < idx_other:
                            keep, drop = m, other
                            break
                # Ensure team_b is populated from the other market's team_a
                if not keep.team_b and drop.team_a:
                    keep.team_b = drop.team_a
                if not keep.team_b:
                    keep.team_b = drop.team_a
                result.append(keep)
            else:
                # 1 market (no pair) or 3+ markets (e.g. with tie) — keep all
                result.extend(group)

        return result

    def _parse_market(self, m: dict, market_type: str = "futures") -> Market | None:
        """Parse a raw Kalshi market dict into a Market object."""
        title = m.get("title", "")
        ticker = m.get("ticker", "")
        event_ticker = m.get("event_ticker", "")
        series_ticker = m.get("series_ticker", "")

        # Detect spread/OU subtype and line value
        market_subtype, line_value = _detect_market_subtype(ticker, title)

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

        # Try to fill team_b from "X vs Y" in title if still empty
        if not team_b:
            _, extracted_b = self._extract_both_teams_from_title(title)
            if extracted_b:
                team_b = extracted_b

        # Align team_a = YES team using yes_sub_title from API
        yes_sub = m.get("yes_sub_title", "")
        if team_b:
            team_a, team_b = self._align_yes_team_v2(
                team_a, team_b, yes_sub, ticker, event_ticker,
            )

        no_sub = m.get("no_sub_title", "")
        rules = m.get("rules_primary", "")

        # Handle Tie/Draw markets for 3-way arbitrage (soccer)
        is_tie_market = ticker.endswith("-TIE") or no_sub.lower() == "tie"
        if is_tie_market:
            yes_team = "Draw"
            market_subtype = "draw"
            line_value = None
        else:
            yes_team = self._extract_team_from_rules(rules)
            if not yes_team:
                yes_team = no_sub
            # Detect spread/over_under/moneyline from ticker
            market_subtype, line_value = _detect_market_subtype(ticker, title)

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

        # Detect esports map number
        map_num = None
        if sport == "esports":
            map_num = _detect_map_number(ticker, title)
            if map_num:
                market_subtype = "map_winner"

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
            line=line_value,
            map_number=map_num,
            url=f"https://kalshi.com/markets/{(series_ticker or event_ticker.split('-')[0]).lower()}/e/{event_ticker.lower()}",
            raw_data={
                "yes_team": yes_team,
                "no_sub_title": no_sub,
                "event_ticker": event_ticker,
                "series_ticker": series_ticker,
                "market_subtype": market_subtype,
                "close_time": m.get("close_time"),
                "expiration_time": m.get("expiration_time"),
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
                no_bid=round(1 - ya, 4) if ya else None,
                no_ask=round(1 - yb, 4) if yb else None,
                volume=m.get("volume", 0),
                last_updated=datetime.now(UTC),
            )

        return market

    async def fetch_price(self, market_id: str) -> MarketPrice | None:
        """Fetch price for a single Kalshi market."""
        try:
            resp = await self._request_with_retry("GET", f"/markets/{market_id}")
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

    async def poll_active_markets(self, market_ids: list[str]) -> dict[str, MarketPrice]:
        """Batch fetch fresh prices for specific market IDs (used between full scans)."""
        results: dict[str, MarketPrice] = {}
        if not market_ids:
            return results

        _sem = asyncio.Semaphore(5)

        async def _fetch_one(mid: str) -> tuple[str, MarketPrice | None]:
            async with _sem:
                price = await self.fetch_price(mid)
                return mid, price

        tasks = [_fetch_one(mid) for mid in market_ids]
        fetched = await asyncio.gather(*tasks, return_exceptions=True)
        for res in fetched:
            if isinstance(res, Exception):
                continue
            mid, price = res
            if price:
                results[mid] = price
        return results

    @staticmethod
    def _align_yes_team_v2(
        team_a: str, team_b: str, yes_sub_title: str,
        ticker: str, event_ticker: str,
    ) -> tuple[str, str]:
        """Align team_a = YES team, using API's yes_sub_title as primary signal.

        Falls back to ticker suffix matching if yes_sub_title is empty.
        """
        if not team_a or not team_b:
            return team_a, team_b

        if yes_sub_title:
            ys = yes_sub_title.lower().strip()
            a_low = team_a.lower().strip()
            b_low = team_b.lower().strip()
            # Check exact or substring match
            a_match = (ys == a_low) or (ys in a_low) or (a_low in ys)
            b_match = (ys == b_low) or (ys in b_low) or (b_low in ys)
            if b_match and not a_match:
                return team_b, team_a
            if a_match:
                return team_a, team_b

            # yes_sub_title often has abbreviated prefix: "NSH Predators", "STL Blues"
            # Strip the 2-4 letter prefix and try matching the remainder
            ys_parts = ys.split(None, 1)
            if len(ys_parts) == 2:
                ys_suffix = ys_parts[1]  # e.g. "predators", "blues"
                a_match2 = (ys_suffix in a_low) or (a_low in ys_suffix)
                b_match2 = (ys_suffix in b_low) or (b_low in ys_suffix)
                if b_match2 and not a_match2:
                    return team_b, team_a
                if a_match2:
                    return team_a, team_b

            # Fuzzy fallback using token similarity
            try:
                from rapidfuzz import fuzz as _fuzz
                sim_a = _fuzz.token_sort_ratio(ys, a_low)
                sim_b = _fuzz.token_sort_ratio(ys, b_low)
                if sim_b > sim_a and sim_b > 50:
                    return team_b, team_a
                if sim_a > sim_b and sim_a > 50:
                    return team_a, team_b
            except ImportError:
                pass
            # Neither matched — fall through to ticker-based

        return KalshiConnector._align_yes_team(
            team_a, team_b, ticker, event_ticker,
        )

    @staticmethod
    def _align_yes_team(
        team_a: str, team_b: str, ticker: str, event_ticker: str,
    ) -> tuple[str, str]:
        """Ensure team_a is the YES team based on ticker suffix.

        Kalshi market tickers encode the YES team as a suffix:
          market ticker: KXEPLGAME-26FEB01AVLBRE-BRE  (YES=Brentford)

        We match the suffix against team names directly to determine which
        team is YES, rather than relying on event ticker position (which
        breaks when team_a was set by _extract_team_from_question).
        """
        if not team_a or not team_b or not ticker or not event_ticker:
            return team_a, team_b

        parts = ticker.split("-")
        if len(parts) < 2:
            return team_a, team_b
        suffix = parts[-1].upper()
        if suffix in ("TIE", "DRAW"):
            return team_a, team_b

        # Check which team name contains the suffix as a substring
        a_upper = team_a.upper().replace(" ", "")
        b_upper = team_b.upper().replace(" ", "")
        a_matches = suffix in a_upper or a_upper.startswith(suffix)
        b_matches = suffix in b_upper or b_upper.startswith(suffix)

        if b_matches and not a_matches:
            # team_b is the YES team → swap so team_a = YES team
            return team_b, team_a
        # team_a already matches or ambiguous → keep as-is
        return team_a, team_b

    @staticmethod
    def _parse_teams(title: str) -> tuple[str, str]:
        """Extract two team names from 'Team A vs Team B Winner?' or 'Team A at Team B Winner?' style title."""
        for sep in (" vs. ", " vs ", " v. ", " v ", " at "):
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
    def _extract_both_teams_from_title(title: str) -> tuple[str, str]:
        """Extract both teams from title containing 'X vs Y' or 'X vs. Y'.

        Handles patterns like:
          "Will X win the X vs Y : Round match?"
          "Will X win the X vs. Y match?"
          "X vs Y Winner?"
        Returns (team_a, team_b) or ("", "") if no match.
        """
        # Look for "vs" / "vs." / "at" inside the title
        m = re.search(r"([\w\s\.\-']+?)\s+(?:vs\.?|at)\s+([\w\s\.\-']+?)(?:\s*[:\?]|\s+(?:Winner|Game|Match|Round)|\s*$)", title, re.IGNORECASE)
        if m:
            a = m.group(1).strip()
            b = m.group(2).strip()
            # Clean up: remove leading "the"
            for prefix in ("the ", "The "):
                if a.startswith(prefix):
                    a = a[len(prefix):]
                if b.startswith(prefix):
                    b = b[len(prefix):]
            if len(a) > 1 and len(b) > 1:
                return a, b
        return "", ""

    @staticmethod
    def _extract_team_from_rules(rules: str) -> str:
        """Extract the YES team from rules like 'If [Team] wins the...'"""
        m = re.match(r"^If\s+(.+?)\s+wins\s+the\b", rules, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return ""

    # ========== TRADING METHODS ==========

    async def get_balance(self) -> float:
        """Get available USD balance on Kalshi."""
        if not self._http:
            raise RuntimeError("Kalshi connector not connected")

        # Use _request_with_retry which correctly signs the full path
        resp = await self._request_with_retry("GET", "/portfolio/balance")
        data = resp.json()
        # Balance is in cents
        balance_cents = data.get("balance", 0)
        return balance_cents / 100

    async def place_order(
        self,
        ticker: str,
        side: str,  # "yes" or "no"
        action: str,  # "buy" or "sell"
        count: int,  # number of contracts
        price_cents: int,  # price in cents (1-99)
        time_in_force: str = "fill_or_kill",  # or "gtc", "ioc"
    ) -> dict:
        """Place an order on Kalshi.

        Args:
            ticker: Market ticker (e.g. "KXNBA-26FEB04-LAL")
            side: "yes" or "no"
            action: "buy" or "sell"
            count: Number of contracts
            price_cents: Price in cents (1-99)
            time_in_force: "fill_or_kill", "gtc", or "ioc"

        Returns:
            Dict with order_id, status, etc.
        """
        if not self._http:
            raise RuntimeError("Kalshi connector not connected")

        order_data = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
            "yes_price" if side == "yes" else "no_price": price_cents,
            "time_in_force": time_in_force,
        }

        try:
            # Sign request with full path (including /trade-api/v2 prefix)
            req = self._http.build_request("POST", "/portfolio/orders", json=order_data)
            full_path = req.url.raw_path.decode()
            auth_headers = self._sign_request("POST", full_path)

            resp = await self._http.post(
                "/portfolio/orders",
                json=order_data,
                headers=auth_headers,
            )

            if resp.status_code == 201 or resp.status_code == 200:
                data = resp.json()
                order = data.get("order", {})
                return {
                    "order_id": order.get("order_id"),
                    "status": order.get("status"),
                    "ticker": order.get("ticker"),
                    "side": order.get("side"),
                    "action": order.get("action"),
                }
            else:
                error_data = resp.json() if resp.content else {}
                return {
                    "error": error_data.get("message", f"HTTP {resp.status_code}"),
                    "status": "failed",
                }

        except Exception as e:
            logger.error(f"Kalshi place_order failed: {e}")
            return {"error": str(e), "status": "failed"}

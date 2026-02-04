from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, date, datetime
from typing import AsyncIterator

import httpx
import websockets

from src.config import settings
from src.connectors.base import BaseConnector
from src.models import Market, MarketPrice, Platform

logger = logging.getLogger(__name__)

# Map keywords in Polymarket event titles / tags to sport codes
_POLY_SPORT_KEYWORDS: dict[str, str] = {
    "nba": "nba", "ncaa men's basketball": "ncaa_mb", "ncaa women's basketball": "ncaa_wb",
    "nfl": "nfl", "ncaa football": "ncaa_fb", "college football": "ncaa_fb",
    "nhl": "nhl",
    "mlb": "mlb",
    "premier league": "soccer", "epl": "soccer", "la liga": "soccer",
    "bundesliga": "soccer", "serie a": "soccer", "ligue 1": "soccer",
    "champions league": "soccer", "saudi pro": "soccer", "ekstraklasa": "soccer",
    "süper lig": "soccer", "super lig": "soccer", "belgian pro": "soccer",
    "fifa": "soccer", "fifa world cup": "soccer", "men's world cup": "soccer",
    "women's world cup": "soccer", "mls": "soccer",
    "ufc": "mma", "mma": "mma",
    "atp": "tennis", "wta": "tennis", "grand slam": "tennis",
    "french open": "tennis", "wimbledon": "tennis", "us open tennis": "tennis",
    "australian open": "tennis",
    # ATP/WTA tournament names for individual match detection
    "transylvania open": "tennis", "ostrava open": "tennis",
    "abu dhabi open": "tennis", "adelaide international": "tennis",
    "brisbane international": "tennis", "qatar open": "tennis",
    "dubai open": "tennis", "dubai tennis": "tennis",
    "indian wells": "tennis", "miami open": "tennis",
    "monte carlo": "tennis", "madrid open": "tennis",
    "rome open": "tennis", "italian open": "tennis",
    "halle open": "tennis", "queen's club": "tennis",
    "canadian open": "tennis", "cincinnati open": "tennis",
    "shanghai masters": "tennis", "vienna open": "tennis",
    "basel open": "tennis", "paris masters": "tennis",
    "roland garros": "tennis", "open sud": "tennis",
    "open 13": "tennis", "lyon open": "tennis",
    "montpellier open": "tennis", "marseille open": "tennis",
    "dallas open": "tennis", "delray beach": "tennis",
    "los cabos open": "tennis", "atlanta open": "tennis",
    "washington open": "tennis", "winston-salem": "tennis",
    "stockholm open": "tennis", "antwerp open": "tennis",
    "metz open": "tennis", "korea open": "tennis",
    "japan open": "tennis", "china open": "tennis",
    "hong kong open": "tennis", "auckland open": "tennis",
    "hobart international": "tennis", "linz open": "tennis",
    "doha open": "tennis", "stuttgart open": "tennis",
    "berlin open": "tennis", "eastbourne international": "tennis",
    "nottingham open": "tennis", "birmingham classic": "tennis",
    "san diego open": "tennis", "guadalajara open": "tennis",
    "zhengzhou open": "tennis", "wuhan open": "tennis",
    "beijing open": "tennis", "moscow open": "tennis",
    "nitto atp finals": "tennis", "wta finals": "tennis",
    "davis cup": "tennis", "billie jean king cup": "tennis",
    "united cup": "tennis", "laver cup": "tennis",
    "round of 128": "tennis", "round of 64": "tennis",
    "round of 32": "tennis", "round of 16": "tennis",
    "cricket": "cricket", "icc": "cricket", "ipl": "cricket", "test match": "cricket",
    "t20": "cricket", "ashes": "cricket",
    # Compound cricket keywords (longer than "world cup" so they win in sorted order)
    "icc men's t20 world cup": "cricket", "icc women's t20 world cup": "cricket",
    "t20 world cup": "cricket", "icc world cup": "cricket",
    "icc men's": "cricket", "icc women's": "cricket",
    "cricket world cup": "cricket",
    "copa": "soccer", "europa league": "soccer", "carabao": "soccer",
    "fa cup": "soccer", "eredivisie": "soccer", "liga mx": "soccer", "j-league": "soccer",
    "efl": "soccer", "league one": "soccer", "league two": "soccer",
    "championship": "soccer",  # EFL Championship
    # Rugby
    "top 14": "rugby", "rugby": "rugby", "six nations": "rugby",
    "premiership rugby": "rugby", "united rugby": "rugby",
    "pga": "golf", "lpga": "golf", "masters": "golf",
    "f1": "motorsport", "formula": "motorsport", "nascar": "motorsport", "indycar": "motorsport",
    "chess": "chess",
    "boxing": "boxing", "fight night": "boxing",
    "pickleball": "pickleball",
    "table tennis": "table_tennis", "ping pong": "table_tennis",
    "tgl": "golf", "dp world": "golf",
    "swiss super league": "soccer", "swiss league": "soccer",
    "cs2": "esports", "counter-strike": "esports", "lol": "esports",
    "valorant": "esports", "dota": "esports", "league of legends": "esports",
    # Olympics
    "winter olympics": "olympics", "summer olympics": "olympics", "olympic": "olympics",
    # Futures-specific keywords (Fix 5)
    "nba finals": "nba", "nba champion": "nba", "nba mvp": "nba",
    "nba eastern": "nba", "nba western": "nba", "nba rookie": "nba",
    "nba defensive": "nba",
    "super bowl": "nfl", "nfl mvp": "nfl",
    "stanley cup": "nhl", "nhl champion": "nhl",
    "world series": "mlb", "mlb mvp": "mlb",
    "heisman": "ncaa_fb", "march madness": "ncaa_mb",
    # College conferences → ncaa_mb
    "acc ": "ncaa_mb", "big 10": "ncaa_mb", "big ten": "ncaa_mb",
    "big 12": "ncaa_mb", "big twelve": "ncaa_mb", "big east": "ncaa_mb",
    "sec ": "ncaa_mb", "pac-12": "ncaa_mb", "pac 12": "ncaa_mb",
    "mountain west": "ncaa_mb", "american athletic": "ncaa_mb",
    "atlantic 10": "ncaa_mb", "west coast conference": "ncaa_mb",
    "missouri valley": "ncaa_mb", "colonial athletic": "ncaa_mb",
    "cwbb": "ncaa_wb", "women's college basketball": "ncaa_wb", "wcbb": "ncaa_wb",
    "ncaa": "ncaa_mb",
    # AHL / minor hockey
    "ahl": "nhl",
}

# Pre-sorted keywords: longer (more specific) first to avoid false matches
# e.g. "icc" and "t20" must match before "world cup"
_POLY_SPORT_KEYWORDS_SORTED: list[tuple[str, str]] = sorted(
    _POLY_SPORT_KEYWORDS.items(), key=lambda x: -len(x[0])
)


def _detect_sport_poly(event_title: str, tags: list[str] | None = None) -> str:
    """Detect sport from Polymarket event title and tags.

    Uses keywords sorted by length descending so specific terms like "icc",
    "t20", "nba finals" match before generic ones like "world cup".
    Falls back to heuristics for club name suffixes and college patterns.
    """
    text = event_title.lower()
    if tags:
        text += " " + " ".join(t.lower() for t in tags)
    for keyword, sport in _POLY_SPORT_KEYWORDS_SORTED:
        if keyword in text:
            return sport

    # Fallback: tennis tournament pattern — "X Open:" or "X Masters:" in parenthetical
    if re.search(r"\b\w+\s+open\s*:", text):
        return "tennis"
    if re.search(r"\b\w+\s+masters\s*:", text) and "golf" not in text and "pga" not in text:
        return "tennis"

    # Fallback: soccer club suffixes in title
    if re.search(r"\b(fc|sc|afc|cf)\b", text):
        return "soccer"

    return ""


def _parse_line_value(text: str) -> float | None:
    """Extract spread/total line from market text.

    Patterns:
      "Lakers -3.5", "+3.5", "Over 220.5", "Under 220.5"
      "... by 3.5 or more"
    """
    # Spread: +/-N.5
    m = re.search(r"([+-]\d+\.?\d*)", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    # Over/Under: "Over 220.5" or "Under 220.5"
    # Sign convention: Over=positive, Under=negative
    m = re.search(r"(over|under|o/u|total)\s+(\d+\.?\d*)", text, re.IGNORECASE)
    if m:
        try:
            line = float(m.group(2))
            keyword = m.group(1).lower()
            if keyword == "under":
                return -line
            return line
        except ValueError:
            pass
    return None


def _detect_map_number(text: str) -> int | None:
    """Detect esports map number from market title.

    Patterns:
      "Map 1", "Map 2", "map 3", "MAP1", "map-1"
      "Game 1", "Game 2" (LoL/Dota series)
    """
    # "Map N" or "Game N"
    m = re.search(r"(?:map|game)[\s\-]*(\d+)", text, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _parse_game_date_from_question(question: str) -> date | None:
    """Extract date from 'win on 2026-02-01?' pattern."""
    m = re.search(r"on\s+(\d{4}-\d{2}-\d{2})", question)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return None


def _parse_game_date_from_iso(dt_str: str | None) -> date | None:
    """Extract date from ISO datetime string (gameStartTime)."""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _parse_game_date_from_slug(slug: str) -> date | None:
    """Extract date from Polymarket slug like 'cwbb-merc-etnst-2026-02-01'."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", slug)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return None


class PolymarketConnector(BaseConnector):
    # Dynamic tags discovered at runtime (not persisted to code)
    _dynamic_tag_slugs: set[str] = set()

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._ws = None
        self._running = False
        self._trading_client = None  # Lazy-init for trading

    async def connect(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.polymarket_gamma_api,
            timeout=30,
        )
        self._clob = httpx.AsyncClient(
            base_url=settings.polymarket_clob_api,
            timeout=30,
        )
        logger.info("Polymarket connector initialized")

    async def disconnect(self) -> None:
        self._running = False
        if self._http:
            await self._http.aclose()
        if self._clob:
            await self._clob.aclose()
        if self._ws:
            await self._ws.close()

    # Additional tag slugs to fetch — game markets under these tags often fall
    # outside the main "sports" tag pagination window (1000 event limit).
    _EXTRA_TAG_SLUGS = (
        # Soccer leagues
        "premier-league", "la-liga", "bundesliga", "ligue-1", "mls", "soccer",
        # US sports
        "nba", "nfl", "nhl", "mlb",
        # College
        "ncaa-basketball", "cwbb", "ncaa-football",
        # Other sports with moneyline markets
        "tennis", "cricket", "ufc", "boxing", "golf", "pga", "rugby", "f1",
        # Esports
        "counter-strike-2", "league-of-legends", "valorant", "dota-2",
    )

    async def fetch_sports_events(self) -> list[Market]:
        """Fetch sports events from Gamma API.

        Handles two market formats:
        1. negRisk=True (soccer 3-way): separate YES/NO sub-markets per team + draw
        2. negRisk=False (US sports 2-way): single market, outcomes are team names

        Also fetches esports-specific tags to cover game markets that fall
        outside the main "sports" tag pagination window.
        """
        markets: list[Market] = []
        seen_event_ids: set[str] = set()

        # Primary fetch: tag_slug=sports (main coverage)
        sports_markets, sports_seen = await self._fetch_events_by_tag("sports", max_pages=10)
        markets.extend(sports_markets)
        seen_event_ids.update(sports_seen)
        sports_count = len(markets)

        # Secondary fetch: extra tags + dynamic tags in parallel batches of 5
        extra_count = 0
        _tag_sem = asyncio.Semaphore(5)
        all_tags = list(self._EXTRA_TAG_SLUGS) + [t for t in self._dynamic_tag_slugs if t not in set(self._EXTRA_TAG_SLUGS)]

        async def _fetch_tag(tag: str) -> tuple[list[Market], set[str]]:
            async with _tag_sem:
                return await self._fetch_events_by_tag(
                    tag, max_pages=5, seen_event_ids=seen_event_ids,
                )

        tag_results = await asyncio.gather(
            *[_fetch_tag(tag) for tag in all_tags],
            return_exceptions=True,
        )
        for res in tag_results:
            if isinstance(res, Exception):
                logger.warning(f"Tag fetch error: {res}")
                continue
            tag_markets, tag_seen = res
            markets.extend(tag_markets)
            seen_event_ids.update(tag_seen)
            extra_count += len(tag_markets)

        logger.info(
            f"Polymarket: fetched {len(markets)} sports markets "
            f"({sports_count} from sports + {extra_count} from esports tags, "
            f"{len(seen_event_ids)} unique events)"
        )
        return markets

    async def _fetch_events_by_tag(
        self,
        tag_slug: str,
        max_pages: int = 10,
        seen_event_ids: set[str] | None = None,
    ) -> tuple[list[Market], set[str]]:
        """Fetch events for a single tag_slug and parse into Markets.

        Returns (markets, seen_event_ids) for deduplication across tags.
        """
        markets: list[Market] = []
        seen = set[str]()
        offset = 0
        limit = 100

        try:
            for page in range(max_pages):
                resp = await self._http.get(
                    "/events",
                    params={
                        "tag_slug": tag_slug,
                        "active": "true",
                        "closed": "false",
                        "limit": limit,
                        "offset": offset,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                events = data if isinstance(data, list) else data.get("data", data.get("events", []))
                if not events:
                    break

                for event in events:
                    event_id = str(event.get("id", ""))

                    # Skip events already seen from another tag
                    if seen_event_ids and event_id in seen_event_ids:
                        continue
                    if event_id in seen:
                        continue
                    seen.add(event_id)

                    parsed = self._parse_event(event)
                    markets.extend(parsed)

                offset += limit
                if len(events) < limit:
                    break

        except Exception:
            logger.exception(f"Error fetching Polymarket events for tag={tag_slug}")
        return markets, seen

    def _parse_event(self, event: dict) -> list[Market]:
        """Parse a single Gamma API event dict into a list of Market objects."""
        markets: list[Market] = []
        event_title = event.get("title", "")
        event_markets = event.get("markets", [])
        slug = event.get("slug", "")
        event_id = str(event.get("id", ""))
        neg_risk = event.get("negRisk", False)

        # Detect sport from event-level info
        event_tags = [t.get("label", t) if isinstance(t, dict) else str(t)
                      for t in event.get("tags", [])]
        sport = _detect_sport_poly(event_title, event_tags)

        # Fallback: try to detect sport from individual market questions
        if not sport:
            for _m in event_markets:
                q = _m.get("question", "")
                if q:
                    sport = _detect_sport_poly(q)
                    if sport:
                        break

        for m in event_markets:
            sports_type = m.get("sportsMarketType", "")

            # Process moneyline, spread, and over/under markets
            _ALLOWED_SPORTS_TYPES = {"moneyline", "spread", "over-under", "over_under", "total"}
            if sports_type and sports_type not in _ALLOWED_SPORTS_TYPES:
                continue

            # Classify: daily game vs futures
            mtype = "game" if sports_type else "futures"

            # Determine sub-type for spread/OU
            market_subtype = "moneyline"
            if sports_type in ("spread",):
                market_subtype = "spread"
            elif sports_type in ("over-under", "over_under", "total"):
                market_subtype = "over_under"

            outcomes = self._parse_json_field(m.get("outcomes", "[]"))
            prices = self._parse_json_field(m.get("outcomePrices", "[]"))
            tokens = self._parse_json_field(m.get("clobTokenIds", "[]"))

            # Extract volume (Gamma API provides volume as string or number)
            market_volume = 0.0
            for vol_key in ("volume", "volumeNum"):
                try:
                    v = float(m.get(vol_key, 0) or 0)
                    if v > market_volume:
                        market_volume = v
                except (ValueError, TypeError):
                    pass

            if len(outcomes) != 2:
                continue

            question = m.get("question", "")
            group_item_title = m.get("groupItemTitle", "")
            market_id_base = str(m.get("id", m.get("conditionId", "")))
            market_slug = m.get("slug", "")

            # Extract game_date for daily games
            game_date = None
            if mtype == "game":
                game_date = (
                    _parse_game_date_from_question(question)
                    or _parse_game_date_from_iso(m.get("gameStartTime"))
                    or _parse_game_date_from_iso(m.get("endDate") or event.get("endDate"))
                    or _parse_game_date_from_slug(market_slug or slug)
                )

            # Event group for futures matching
            event_group = event_title if mtype == "futures" else ""

            # Parse line value for spread/OU markets
            line_value = None
            if market_subtype in ("spread", "over_under"):
                line_value = _parse_line_value(question) or _parse_line_value(group_item_title)

            # Detect esports map number
            map_num = None
            if sport == "esports":
                map_num = (
                    _detect_map_number(event_title)
                    or _detect_map_number(question)
                    or _detect_map_number(group_item_title)
                )
                if map_num:
                    market_subtype = "map_winner"

            if neg_risk:
                # 3-way soccer or multi-outcome futures:
                # each sub-market is YES/NO for one team
                is_draw = "draw" in group_item_title.lower() or "draw" in question.lower()
                if is_draw:
                    # Store draw markets for 3-way arbitrage
                    team_name = "Draw"
                    market_subtype = "draw"
                else:
                    team_name = group_item_title or self._extract_team_from_question(question)
                if not team_name:
                    continue

                # Parse opponent (team_b) from event_title "X vs Y"
                team_b_neg = ""
                if event_title:
                    ev_a, ev_b = self._parse_vs_teams(event_title)
                    if ev_a and ev_b:
                        # Figure out which side this sub-market is
                        tn_lower = team_name.lower()
                        if tn_lower in ev_a.lower() or ev_a.lower() in tn_lower:
                            team_b_neg = ev_b
                        elif tn_lower in ev_b.lower() or ev_b.lower() in tn_lower:
                            team_b_neg = ev_a
                        else:
                            # fuzzy fallback
                            from rapidfuzz import fuzz as _fuzz
                            if _fuzz.ratio(tn_lower, ev_a.lower()) > _fuzz.ratio(tn_lower, ev_b.lower()):
                                team_b_neg = ev_b
                            else:
                                team_b_neg = ev_a

                price = self._build_price(prices, volume=market_volume)
                markets.append(Market(
                    platform=Platform.POLYMARKET,
                    market_id=market_id_base,
                    event_id=event_id,
                    title=question,
                    team_a=team_name,
                    team_b=team_b_neg,
                    category="sports",
                    market_type=mtype,
                    sport=sport,
                    game_date=game_date,
                    event_group=event_group,
                    line=line_value,
                    map_number=map_num,
                    url=f"https://polymarket.com/event/{slug}/{market_slug}" if market_slug else f"https://polymarket.com/event/{slug}",
                    price=price,
                    raw_data={
                        "clob_token_ids": tokens,
                        "condition_id": m.get("conditionId", ""),
                        "slug": slug,
                        "event_title": event_title,
                        "outcomes": outcomes,
                        "sports_market_type": sports_type,
                        "market_subtype": market_subtype,
                        "neg_risk": True,
                        "game_start_time": m.get("gameStartTime"),
                        "end_date": m.get("endDate") or event.get("endDate"),
                    },
                ))
            else:
                # 2-way market: outcomes are team names
                # Create one Market per team for cross-platform matching
                other_team = {0: "", 1: ""}
                if len(outcomes) == 2:
                    other_team = {0: outcomes[1], 1: outcomes[0]}

                for i, team_name in enumerate(outcomes):
                    if not team_name or len(team_name) < 2:
                        continue

                    price = None
                    if len(prices) == 2:
                        try:
                            yes_p = float(prices[i])
                            if yes_p > 0:
                                price = MarketPrice(
                                    yes_price=round(yes_p, 4),
                                    no_price=round(1.0 - yes_p, 4),
                                    volume=market_volume,
                                    last_updated=datetime.now(UTC),
                                )
                        except (ValueError, TypeError):
                            pass

                    # Store ALL tokens so arbitrage calculator can access both teams' tokens
                    # outcome_index tells which token index corresponds to this market's team_a
                    markets.append(Market(
                        platform=Platform.POLYMARKET,
                        market_id=f"{market_id_base}_{i}",
                        event_id=event_id,
                        title=f"Will {team_name} win? ({event_title})",
                        team_a=team_name,
                        team_b=other_team.get(i, ""),
                        category="sports",
                        market_type=mtype,
                        sport=sport,
                        game_date=game_date,
                        event_group=event_group,
                        line=line_value,
                        map_number=map_num,
                        url=f"https://polymarket.com/event/{slug}/{market_slug}" if market_slug else f"https://polymarket.com/event/{slug}",
                        price=price,
                        raw_data={
                            "clob_token_ids": tokens,  # Store ALL tokens for both teams
                            "condition_id": m.get("conditionId", ""),
                            "slug": slug,
                            "event_title": event_title,
                            "outcomes": outcomes,
                            "outcome_index": i,  # This market's team_a is at tokens[i]
                            "sports_market_type": sports_type,
                            "market_subtype": market_subtype,
                            "neg_risk": False,
                            "game_start_time": m.get("gameStartTime"),
                            "end_date": m.get("endDate") or event.get("endDate"),
                        },
                    ))

        return markets

    @staticmethod
    def _parse_vs_teams(title: str) -> tuple[str, str]:
        """Extract two team names from 'Team A vs. Team B' or 'Team A at Team B'."""
        for sep in (" vs. ", " vs ", " v. ", " v ", " at ", " @ "):
            if sep in title:
                idx = title.index(sep)
                a = title[:idx].strip()
                b = title[idx + len(sep):].strip()
                # Remove trailing punctuation/metadata
                for suffix in ("?", " Winner", " Game", " Match"):
                    b = b.removesuffix(suffix).strip()
                    a = a.removesuffix(suffix).strip()
                if a and b:
                    return a, b
        return "", ""

    @staticmethod
    def _parse_json_field(value) -> list:
        """Parse a field that may be a JSON string or already a list."""
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return []
        return []

    @staticmethod
    def _build_price(prices: list, volume: float = 0) -> MarketPrice | None:
        """Build MarketPrice from a 2-element price list."""
        if len(prices) != 2:
            return None
        try:
            yes_p = float(prices[0])
            no_p = float(prices[1])
            if yes_p > 0 or no_p > 0:
                return MarketPrice(
                    yes_price=yes_p,
                    no_price=no_p,
                    volume=volume,
                    last_updated=datetime.now(UTC),
                )
        except (ValueError, TypeError):
            pass
        return None

    @staticmethod
    def _extract_team_from_question(question: str) -> str:
        """Extract team/player name from a Polymarket question.

        Common patterns:
          "Will the Oklahoma City Thunder win the 2026 NBA Finals?"
          "Will Spain win the 2026 FIFA World Cup?"
          "Will Querétaro FC win on 2026-02-01?"
          "Will Arsenal win the 2025–26 Champions League?"
        """
        import re
        q = question.strip()

        # Pattern: "Will [the] X win [on/the] YEAR..."
        m = re.match(
            r"^Will\s+(?:the\s+)?(.+?)\s+win\s+(?:on\s+|the\s+)?(?:\d{4})",
            q, re.IGNORECASE,
        )
        if m:
            return m.group(1).strip()

        # Pattern: "Will X win the Y?" (futures)
        m = re.match(
            r"^Will\s+(?:the\s+)?(.+?)\s+win\b", q, re.IGNORECASE
        )
        if m:
            name = m.group(1).strip()
            if len(name) > 2:
                return name

        return ""

    async def fetch_price(
        self, market_id: str, *, neg_risk: bool = False, clob_token_id: str | None = None,
    ) -> MarketPrice | None:
        """Fetch price — CLOB midpoint for normal markets, Gamma API for negRisk.

        When clob_token_id is provided for a negRisk market, try CLOB first
        (gets real midpoint from order book) and fall back to Gamma if CLOB fails.
        """
        if neg_risk:
            if clob_token_id:
                try:
                    resp = await self._clob.get("/midpoint", params={"token_id": clob_token_id})
                    resp.raise_for_status()
                    data = resp.json()
                    mid = float(data.get("mid", 0))
                    if mid > 0:
                        logger.debug(f"CLOB midpoint for negRisk {clob_token_id[:20]}: {mid}")
                        return MarketPrice(
                            yes_price=mid,
                            no_price=round(1 - mid, 4),
                            last_updated=datetime.now(UTC),
                        )
                except Exception:
                    logger.debug(f"CLOB midpoint failed for negRisk {clob_token_id[:20]}, falling back to Gamma")
            return await self._fetch_price_gamma(market_id)
        try:
            resp = await self._clob.get("/midpoint", params={"token_id": market_id})
            resp.raise_for_status()
            data = resp.json()
            mid = float(data.get("mid", 0))
            # Validate midpoint is reasonable (not 0 or 1)
            if mid <= 0 or mid >= 1:
                logger.debug(f"Invalid midpoint {mid} for {market_id[:20]}, falling back to Gamma")
                return await self._fetch_price_gamma(market_id)
            return MarketPrice(
                yes_price=mid,
                no_price=round(1 - mid, 4),
                last_updated=datetime.now(UTC),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug(f"CLOB 404, falling back to Gamma: {market_id[:20]}...")
                return await self._fetch_price_gamma(market_id)
            logger.warning(f"HTTP error fetching price for {market_id[:20]}: {e.response.status_code}")
            return None
        except Exception:
            logger.warning(f"Error fetching price for {market_id[:20]}...")
            return None

    async def _fetch_price_gamma(self, market_id: str) -> MarketPrice | None:
        """Fetch price from Gamma API /markets/{id} (works for negRisk tokens)."""
        try:
            resp = await self._http.get(f"/markets/{market_id}")
            resp.raise_for_status()
            data = resp.json()
            # Gamma API returns outcomePrices as JSON string or list
            prices = self._parse_json_field(data.get("outcomePrices", "[]"))
            vol = 0.0
            for vk in ("volume", "volumeNum"):
                try:
                    v = float(data.get(vk, 0) or 0)
                    if v > vol:
                        vol = v
                except (ValueError, TypeError):
                    pass
            if len(prices) == 2:
                yes_p = float(prices[0])
                no_p = float(prices[1])
                if yes_p > 0 or no_p > 0:
                    return MarketPrice(
                        yes_price=round(yes_p, 4),
                        no_price=round(no_p, 4),
                        yes_bid=float(data.get("bestBid", 0)) or None,
                        yes_ask=float(data.get("bestAsk", 0)) or None,
                        volume=vol,
                        last_updated=datetime.now(UTC),
                    )
            return None
        except Exception:
            logger.debug(f"Gamma price fetch failed for {market_id[:20]}...")
            return None

    async def fetch_prices_batch(self, token_ids: list[str]) -> dict[str, MarketPrice]:
        """Fetch prices for multiple tokens via POST /prices."""
        result: dict[str, MarketPrice] = {}
        if not token_ids:
            return result
        # API limit: 500 tokens per request
        CHUNK_SIZE = 500
        for i in range(0, len(token_ids), CHUNK_SIZE):
            chunk = token_ids[i:i + CHUNK_SIZE]
            try:
                body = [{"token_id": tid, "side": "BUY"} for tid in chunk]
                resp = await self._clob.post("/prices", json=body)
                resp.raise_for_status()
                data = resp.json()
                # Response: {token_id: {"BUY": price_str, "SELL": price_str}, ...}
                for tid in chunk:
                    if tid not in data:
                        continue
                    try:
                        entry = data[tid]
                        if isinstance(entry, dict):
                            p = float(entry.get("BUY", 0))
                        else:
                            p = float(entry)
                        if p > 0:
                            result[tid] = MarketPrice(
                                yes_price=p,
                                no_price=round(1 - p, 4),
                                last_updated=datetime.now(UTC),
                            )
                    except (ValueError, TypeError):
                        continue
            except Exception:
                logger.exception(f"Error batch fetching Polymarket prices (chunk {i})")
        return result

    async def fetch_book(self, token_id: str) -> MarketPrice | None:
        """Fetch order book with full depth for bid/ask spread and liquidity analysis."""
        from src.models import OrderBookLevel, OrderBookDepth

        try:
            resp = await self._clob.get(f"/book", params={"token_id": token_id})
            resp.raise_for_status()
            data = resp.json()
            raw_bids = data.get("bids", [])
            raw_asks = data.get("asks", [])

            # Parse into OrderBookLevel objects with full depth
            # Bids sorted by price descending (best/highest first)
            bids = sorted(
                [OrderBookLevel(price=float(b["price"]), size=float(b["size"])) for b in raw_bids],
                key=lambda x: -x.price,
            )
            # Asks sorted by price ascending (best/lowest first)
            asks = sorted(
                [OrderBookLevel(price=float(a["price"]), size=float(a["size"])) for a in raw_asks],
                key=lambda x: x.price,
            )

            # Create YES depth
            yes_depth = OrderBookDepth(bids=bids, asks=asks)

            # Derive NO depth by inverting prices
            # NO bid = 1 - YES ask, NO ask = 1 - YES bid
            no_bids = sorted(
                [OrderBookLevel(price=round(1.0 - a.price, 4), size=a.size) for a in asks],
                key=lambda x: -x.price,
            )
            no_asks = sorted(
                [OrderBookLevel(price=round(1.0 - b.price, 4), size=b.size) for b in bids],
                key=lambda x: x.price,
            )
            no_depth = OrderBookDepth(bids=no_bids, asks=no_asks)

            best_bid = yes_depth.best_bid
            best_ask = yes_depth.best_ask

            # Debug: log book state for diagnosis
            if not raw_bids or not raw_asks:
                logger.debug(
                    f"Book for {token_id[:20]}: empty - bids={len(raw_bids)}, asks={len(raw_asks)}"
                )

            # Filter out junk orders: bid/ask spread > 90% means empty book
            if best_bid is not None and best_ask is not None:
                spread = best_ask - best_bid
                if spread > 0.90:
                    logger.debug(
                        f"Book for {token_id[:20]}: junk spread "
                        f"{best_bid}/{best_ask} ({spread:.0%}), treating as empty"
                    )
                    best_bid = None
                    best_ask = None
                    yes_depth = None
                    no_depth = None

            # Compute midpoint only from meaningful bid/ask
            if best_bid is not None and best_ask is not None:
                mid = (best_bid + best_ask) / 2
            elif best_bid is not None:
                mid = best_bid
            elif best_ask is not None:
                mid = best_ask
            else:
                # Truly empty book — fall back to /midpoint
                logger.info(f"Book empty for {token_id[:20]}, falling back to midpoint API")
                mid_resp = await self._clob.get("/midpoint", params={"token_id": token_id})
                mid_resp.raise_for_status()
                mid = float(mid_resp.json().get("mid", 0))
                yes_depth = None
                no_depth = None

            if mid <= 0:
                return None

            return MarketPrice(
                yes_price=round(mid, 4),
                no_price=round(1 - mid, 4),
                yes_bid=best_bid,
                yes_ask=best_ask,
                no_bid=round(1 - best_ask, 4) if best_ask else None,
                no_ask=round(1 - best_bid, 4) if best_bid else None,
                yes_depth=yes_depth,
                no_depth=no_depth,
                last_updated=datetime.now(UTC),
            )
        except Exception:
            logger.exception(f"Error fetching book for {token_id}")
            return None

    # Known sports-related tag patterns for discovery
    _KNOWN_SPORTS_PATTERNS = {
        "sports", "nba", "nfl", "nhl", "mlb", "soccer", "football", "tennis",
        "cricket", "ufc", "mma", "boxing", "golf", "pga", "rugby", "f1",
        "motorsport", "esports", "ncaa", "basketball", "hockey", "baseball",
        "premier-league", "la-liga", "bundesliga", "serie-a", "ligue-1",
        "champions-league", "mls", "pickleball", "chess",
    }

    async def discover_sports_tags(self) -> list[str]:
        """Discover new sports tags from active Polymarket events.

        Fetches recent events without tag filter, collects all tags,
        and logs any new ones not in _EXTRA_TAG_SLUGS.
        Auto-adds qualifying tags (>= 3 events) to _dynamic_tag_slugs.
        Returns list of newly discovered tags.
        """
        known_tags = set(self._EXTRA_TAG_SLUGS) | self._dynamic_tag_slugs
        tag_event_counts: dict[str, int] = {}
        new_tags: list[str] = []

        try:
            for offset in range(0, 500, 100):
                resp = await self._http.get(
                    "/events",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": 100,
                        "offset": offset,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                events = data if isinstance(data, list) else data.get("data", data.get("events", []))
                if not events:
                    break

                for event in events:
                    tags = event.get("tags", [])
                    for tag in tags:
                        slug = tag.get("slug", tag) if isinstance(tag, dict) else str(tag)
                        slug_lower = slug.lower()
                        tag_event_counts[slug_lower] = tag_event_counts.get(slug_lower, 0) + 1

                if len(events) < 100:
                    break

            # Check for sports-related tags not in our known set
            for tag, count in tag_event_counts.items():
                if tag in known_tags:
                    continue
                # Check if this tag overlaps with known sports patterns
                for pattern in self._KNOWN_SPORTS_PATTERNS:
                    if pattern in tag or tag in pattern:
                        # Only add tags with >= 3 active events
                        if count >= 3:
                            new_tags.append(tag)
                            self._dynamic_tag_slugs.add(tag)
                            logger.warning(f"Auto-added Polymarket tag: {tag} ({count} events)")
                        break

            if new_tags:
                logger.info(
                    f"Tag discovery: {len(new_tags)} new tags added, "
                    f"{len(self._dynamic_tag_slugs)} total dynamic tags"
                )
            else:
                logger.debug(f"Tag discovery: scanned {len(tag_event_counts)} tags, no new sports tags")

        except Exception:
            logger.exception("Error during tag discovery")

        return new_tags

    async def subscribe_prices(self, market_ids: list[str]) -> AsyncIterator[tuple[str, MarketPrice]]:
        """Subscribe to WebSocket price changes using Polymarket CLOB WS protocol."""
        if not market_ids:
            return

        self._running = True
        ws_url = settings.polymarket_ws_url
        reconnect_delay = 2  # Start with 2 second delay
        max_delay = 60  # Cap at 60 seconds

        while self._running:
            try:
                async with websockets.connect(ws_url) as ws:
                    reconnect_delay = 2  # Reset delay on successful connection
                    self._ws = ws
                    # Single subscription message with all asset IDs
                    sub_msg = json.dumps({
                        "type": "MARKET",
                        "assets_ids": market_ids,
                        "custom_feature_enabled": True,
                    })
                    await ws.send(sub_msg)

                    logger.info(f"Polymarket WS: subscribing to {len(market_ids)} tokens")

                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            parsed = json.loads(raw_msg)
                            # Handle both single message and array of messages
                            messages = parsed if isinstance(parsed, list) else [parsed]

                            for msg in messages:
                                if not isinstance(msg, dict):
                                    continue

                                event_type = msg.get("event_type", msg.get("type", ""))

                                if event_type == "price_change":
                                    # price_change events have nested price_changes array
                                    for change in msg.get("price_changes", []):
                                        token_id = change.get("asset_id", "")
                                        price = float(change.get("price", 0))
                                        if token_id and price > 0:
                                            mp = MarketPrice(
                                                yes_price=price,
                                                no_price=round(1 - price, 4),
                                                last_updated=datetime.now(UTC),
                                            )
                                            # Include bid/ask if available
                                            bb = change.get("best_bid")
                                            ba = change.get("best_ask")
                                            if bb:
                                                mp.yes_bid = float(bb)
                                            if ba:
                                                mp.yes_ask = float(ba)
                                            yield (token_id, mp)

                                elif event_type == "best_bid_ask":
                                    # best_bid_ask events (enabled by custom_feature_enabled)
                                    token_id = msg.get("asset_id", "")
                                    bb = msg.get("best_bid")
                                    ba = msg.get("best_ask")
                                    if token_id and (bb or ba):
                                        bid = float(bb) if bb else None
                                        ask = float(ba) if ba else None
                                        mid = ((bid or 0) + (ask or 0)) / 2 if bid and ask else (bid or ask or 0)
                                        if mid > 0:
                                            yield (
                                                token_id,
                                                MarketPrice(
                                                    yes_price=round(mid, 4),
                                                    no_price=round(1 - mid, 4),
                                                    yes_bid=bid,
                                                    yes_ask=ask,
                                                    last_updated=datetime.now(UTC),
                                                ),
                                            )

                        except (json.JSONDecodeError, ValueError):
                            continue

            except websockets.ConnectionClosed:
                logger.warning(f"Polymarket WS disconnected, reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_delay)  # Exponential backoff
            except Exception:
                logger.exception("Polymarket WS error")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_delay)  # Exponential backoff

    # ========== TRADING METHODS ==========

    def _ensure_trading_client(self):
        """Lazily initialize the py-clob-client for trading."""
        if self._trading_client is None:
            from py_clob_client.client import ClobClient

            if not settings.poly_private_key:
                raise ValueError("POLY_PRIVATE_KEY not configured")

            self._trading_client = ClobClient(
                host=settings.polymarket_clob_api,
                key=settings.poly_private_key,
                chain_id=137,  # Polygon mainnet
                signature_type=2,  # Browser wallet proxy
                funder=settings.poly_funder_address or None,
            )
            # Derive API credentials
            self._trading_client.set_api_creds(
                self._trading_client.create_or_derive_api_creds()
            )
        return self._trading_client

    async def get_balance(self) -> float:
        """Get USDC balance on Polymarket."""
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

        client = self._ensure_trading_client()
        result = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        # Balance is in 6-decimal USDC format as string
        balance_raw = result.get("balance", "0")
        return float(balance_raw) / 1_000_000

    async def place_order(
        self,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        price: float,
        size: float,
        order_type: str = "GTC",  # "GTC" or "FOK"
    ) -> dict:
        """Place an order on Polymarket.

        Args:
            token_id: The CLOB token ID
            side: "BUY" or "SELL"
            price: Price per share (0-1)
            size: Number of shares/contracts
            order_type: "GTC" (good till cancelled) or "FOK" (fill or kill)

        Returns:
            Dict with success, orderID, errorMsg etc.
        """
        from py_clob_client.order_builder.constants import BUY, SELL

        client = self._ensure_trading_client()

        # Convert side to py-clob-client constant
        side_const = BUY if side.upper() == "BUY" else SELL

        try:
            if order_type == "FOK":
                # Market order with FOK (fill or kill)
                from py_clob_client.clob_types import MarketOrderArgs

                order_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=size,
                    price=price,
                    side=side_const,
                )
                signed_order = client.create_market_order(order_args)
                result = client.post_order(signed_order, "FOK")
            else:
                # Limit order GTC
                from py_clob_client.clob_types import OrderArgs

                order_args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=side_const,
                )
                signed_order = client.create_order(order_args)
                result = client.post_order(signed_order, "GTC")

            return result

        except Exception as e:
            logger.error(f"Polymarket place_order failed: {e}")
            return {"success": False, "errorMsg": str(e)}

    async def get_order(self, order_id: str) -> dict:
        """Get order status by order ID.

        Args:
            order_id: The order ID returned from place_order

        Returns:
            Dict with order status, matchedAmount, etc.
        """
        client = self._ensure_trading_client()
        try:
            result = client.get_order(order_id)
            return result
        except Exception as e:
            logger.error(f"Polymarket get_order failed: {e}")
            return {"error": str(e)}

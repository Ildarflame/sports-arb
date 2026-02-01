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

    # Fallback: soccer club suffixes in title
    if re.search(r"\b(fc|sc|afc|cf)\b", text):
        return "soccer"

    return ""


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


class PolymarketConnector(BaseConnector):
    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._ws = None
        self._running = False

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
        "nba", "nfl", "nhl",
        # Other sports with moneyline markets
        "tennis", "cricket", "ufc", "boxing", "ncaa-basketball",
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

        # Secondary fetch: esports-specific tags (covers CS2, LoL, Valorant, Dota2 games)
        extra_count = 0
        for tag in self._EXTRA_TAG_SLUGS:
            tag_markets, tag_seen = await self._fetch_events_by_tag(
                tag, max_pages=5, seen_event_ids=seen_event_ids,
            )
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

            # Only process moneyline markets for arbitrage
            if sports_type and sports_type != "moneyline":
                continue

            # Classify: daily game vs futures
            mtype = "game" if sports_type else "futures"

            outcomes = self._parse_json_field(m.get("outcomes", "[]"))
            prices = self._parse_json_field(m.get("outcomePrices", "[]"))
            tokens = self._parse_json_field(m.get("clobTokenIds", "[]"))

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
                )

            # Event group for futures matching
            event_group = event_title if mtype == "futures" else ""

            if neg_risk:
                # 3-way soccer or multi-outcome futures:
                # each sub-market is YES/NO for one team
                if "draw" in group_item_title.lower() or "draw" in question.lower():
                    continue

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

                # Try harder to get game_date for negRisk game markets
                if mtype == "game" and not game_date:
                    game_date = _parse_game_date_from_iso(
                        m.get("endDate") or event.get("endDate")
                    )

                price = self._build_price(prices)
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
                    url=f"https://polymarket.com/event/{slug}/{market_slug}" if market_slug else f"https://polymarket.com/event/{slug}",
                    price=price,
                    raw_data={
                        "clob_token_ids": tokens,
                        "condition_id": m.get("conditionId", ""),
                        "slug": slug,
                        "event_title": event_title,
                        "outcomes": outcomes,
                        "sports_market_type": sports_type,
                        "neg_risk": True,
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
                                    last_updated=datetime.now(UTC),
                                )
                        except (ValueError, TypeError):
                            pass

                    team_token = [tokens[i]] if i < len(tokens) else tokens
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
                        url=f"https://polymarket.com/event/{slug}/{market_slug}" if market_slug else f"https://polymarket.com/event/{slug}",
                        price=price,
                        raw_data={
                            "clob_token_ids": team_token,
                            "condition_id": m.get("conditionId", ""),
                            "slug": slug,
                            "event_title": event_title,
                            "outcomes": outcomes,
                            "outcome_index": i,
                            "sports_market_type": sports_type,
                            "neg_risk": False,
                        },
                    ))

        return markets

    @staticmethod
    def _parse_vs_teams(title: str) -> tuple[str, str]:
        """Extract two team names from 'Team A vs. Team B' or 'Team A vs Team B'."""
        for sep in (" vs. ", " vs ", " v. ", " v "):
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
    def _build_price(prices: list) -> MarketPrice | None:
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

    async def fetch_price(self, market_id: str, *, neg_risk: bool = False) -> MarketPrice | None:
        """Fetch price — CLOB midpoint for normal markets, Gamma API for negRisk."""
        if neg_risk:
            return await self._fetch_price_gamma(market_id)
        try:
            resp = await self._clob.get("/midpoint", params={"token_id": market_id})
            resp.raise_for_status()
            data = resp.json()
            mid = float(data.get("mid", 0))
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
            if len(prices) == 2:
                yes_p = float(prices[0])
                no_p = float(prices[1])
                if yes_p > 0 or no_p > 0:
                    return MarketPrice(
                        yes_price=round(yes_p, 4),
                        no_price=round(no_p, 4),
                        yes_bid=float(data.get("bestBid", 0)) or None,
                        yes_ask=float(data.get("bestAsk", 0)) or None,
                        last_updated=datetime.now(UTC),
                    )
            return None
        except Exception:
            logger.debug(f"Gamma price fetch failed for {market_id[:20]}...")
            return None

    async def fetch_prices_batch(self, token_ids: list[str]) -> dict[str, MarketPrice]:
        """Fetch prices for multiple tokens."""
        result: dict[str, MarketPrice] = {}
        if not token_ids:
            return result
        try:
            resp = await self._clob.get(
                "/prices",
                params={"token_ids": ",".join(token_ids)},
            )
            resp.raise_for_status()
            data = resp.json()
            # data is typically a dict {token_id: price}
            for tid, price_str in data.items():
                try:
                    p = float(price_str)
                    result[tid] = MarketPrice(
                        yes_price=p,
                        no_price=round(1 - p, 4),
                        last_updated=datetime.now(UTC),
                    )
                except (ValueError, TypeError):
                    continue
        except Exception:
            logger.exception("Error batch fetching Polymarket prices")
        return result

    async def fetch_book(self, token_id: str) -> MarketPrice | None:
        """Fetch order book for bid/ask spread."""
        try:
            resp = await self._clob.get(f"/book", params={"token_id": token_id})
            resp.raise_for_status()
            data = resp.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else 0
            best_ask = float(asks[0]["price"]) if asks else 1
            mid = (best_bid + best_ask) / 2 if (best_bid and best_ask) else best_bid or best_ask
            return MarketPrice(
                yes_price=mid,
                no_price=round(1 - mid, 4),
                yes_bid=best_bid,
                yes_ask=best_ask,
                last_updated=datetime.now(UTC),
            )
        except Exception:
            logger.exception(f"Error fetching book for {token_id}")
            return None

    async def subscribe_prices(self, market_ids: list[str]) -> AsyncIterator[tuple[str, MarketPrice]]:
        """Subscribe to WebSocket price changes."""
        if not market_ids:
            return

        self._running = True
        ws_url = settings.polymarket_ws_url

        while self._running:
            try:
                async with websockets.connect(ws_url) as ws:
                    self._ws = ws
                    # Subscribe to price channels for each market
                    for mid in market_ids:
                        sub_msg = json.dumps({
                            "type": "subscribe",
                            "channel": "price_change",
                            "market": mid,
                        })
                        await ws.send(sub_msg)

                    logger.info(f"Polymarket WS: subscribed to {len(market_ids)} markets")

                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw_msg)
                            if msg.get("type") == "price_change":
                                token_id = msg.get("market", msg.get("asset_id", ""))
                                price = float(msg.get("price", 0))
                                if token_id and price:
                                    yield (
                                        token_id,
                                        MarketPrice(
                                            yes_price=price,
                                            no_price=round(1 - price, 4),
                                            last_updated=datetime.now(UTC),
                                        ),
                                    )
                        except (json.JSONDecodeError, ValueError):
                            continue

            except websockets.ConnectionClosed:
                logger.warning("Polymarket WS disconnected, reconnecting...")
                await asyncio.sleep(2)
            except Exception:
                logger.exception("Polymarket WS error")
                await asyncio.sleep(5)

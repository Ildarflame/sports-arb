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
    "fifa": "soccer", "world cup": "soccer", "mls": "soccer",
    "ufc": "mma", "mma": "mma",
    "atp": "tennis", "wta": "tennis", "grand slam": "tennis",
    "french open": "tennis", "wimbledon": "tennis", "us open tennis": "tennis",
    "australian open": "tennis",
    "cricket": "cricket", "icc": "cricket", "ipl": "cricket", "test match": "cricket",
    "t20": "cricket", "ashes": "cricket",
    "copa": "soccer", "europa league": "soccer", "carabao": "soccer",
    "fa cup": "soccer", "eredivisie": "soccer", "liga mx": "soccer", "j-league": "soccer",
    "pga": "golf", "lpga": "golf", "masters": "golf",
    "f1": "motorsport", "formula": "motorsport", "nascar": "motorsport",
    "cs2": "esports", "lol": "esports", "valorant": "esports", "dota": "esports",
    "league of legends": "esports",
}


def _detect_sport_poly(event_title: str, tags: list[str] | None = None) -> str:
    """Detect sport from Polymarket event title and tags."""
    text = event_title.lower()
    if tags:
        text += " " + " ".join(t.lower() for t in tags)
    for keyword, sport in _POLY_SPORT_KEYWORDS.items():
        if keyword in text:
            return sport
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

    async def fetch_sports_events(self) -> list[Market]:
        """Fetch sports events from Gamma API.

        Handles two market formats:
        1. negRisk=True (soccer 3-way): separate YES/NO sub-markets per team + draw
        2. negRisk=False (US sports 2-way): single market, outcomes are team names
        """
        markets: list[Market] = []
        offset = 0
        limit = 100
        max_pages = 10

        try:
            for page in range(max_pages):
                resp = await self._http.get(
                    "/events",
                    params={
                        "tag_slug": "sports",
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

                            price = self._build_price(prices)
                            markets.append(Market(
                                platform=Platform.POLYMARKET,
                                market_id=market_id_base,
                                event_id=event_id,
                                title=question,
                                team_a=team_name,
                                team_b="",
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
                                    team_b="",
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

                offset += limit
                if len(events) < limit:
                    break

            logger.info(
                f"Polymarket: fetched {len(markets)} sports markets "
                f"({page + 1} pages, {offset} events scanned)"
            )
        except Exception:
            logger.exception("Error fetching Polymarket sports events")
        return markets

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

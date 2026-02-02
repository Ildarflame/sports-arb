from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from src.db import db
from src.models import Platform
from src.web.app import templates

logger = logging.getLogger(__name__)
router = APIRouter()

# Global event queue for SSE broadcasts
_sse_subscribers: list[asyncio.Queue] = []


def broadcast_event(event_type: str, data: dict) -> None:
    """Push event to all SSE subscribers."""
    msg = json.dumps({"type": event_type, **data})
    for q in _sse_subscribers:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


def _get_event_sport(event) -> str:
    """Extract sport from a SportEvent's markets."""
    pm = event.markets.get(Platform.POLYMARKET)
    km = event.markets.get(Platform.KALSHI)
    return (pm.sport if pm else "") or (km.sport if km else "") or ""


def _compute_best_roi(event) -> float:
    """Compute the best ROI for sorting. Returns -999 for non-computable."""
    pm = event.markets.get(Platform.POLYMARKET)
    km = event.markets.get(Platform.KALSHI)
    if not (event.matched and pm and pm.price and km and km.price):
        return -999.0

    cost1 = pm.price.yes_price + km.price.no_price
    fee1 = pm.price.yes_price * 0.02 + km.price.no_price * 0.015
    roi1 = ((1.0 - cost1 - fee1) / (cost1 + fee1) * 100) if cost1 + fee1 > 0 else -100

    cost2 = km.price.yes_price + pm.price.no_price
    fee2 = km.price.yes_price * 0.015 + pm.price.no_price * 0.02
    roi2 = ((1.0 - cost2 - fee2) / (cost2 + fee2) * 100) if cost2 + fee2 > 0 else -100

    return max(roi1, roi2)


def _is_placeholder_price(price) -> bool:
    """Check if price is a 50/50 placeholder (no real data)."""
    if not price:
        return True
    return (price.yes_price == 0.5
            and price.no_price == 0.5
            and price.yes_bid is None
            and price.yes_ask is None
            and (price.volume or 0) == 0)


def _is_stale_event(event) -> bool:
    """Check if a game event's date is in the past (>1 day old)."""
    pm = event.markets.get(Platform.POLYMARKET)
    km = event.markets.get(Platform.KALSHI)
    game_date = (pm.game_date if pm else None) or (km.game_date if km else None)
    if game_date and game_date < date.today() - timedelta(days=1):
        return True
    return False


def _filter_and_sort_events(events: list, sport: str = "", min_roi: float | None = None) -> list:
    """Filter events by sport/min_roi and sort: matched by ROI desc, then Kalshi-only."""
    if sport:
        events = [e for e in events if _get_event_sport(e) == sport]

    # Remove stale events and placeholder-price junk
    cleaned = []
    for e in events:
        if _is_stale_event(e):
            continue
        pm = e.markets.get(Platform.POLYMARKET)
        km = e.markets.get(Platform.KALSHI)
        # Skip events where Poly price is a 50/50 placeholder (no volume, no book)
        if pm and _is_placeholder_price(pm.price):
            continue
        # Skip extreme Kalshi prices (<=2c or >=98c) — illiquid long-shot futures
        if km and km.price and (km.price.yes_price <= 0.02 or km.price.yes_price >= 0.98):
            continue
        cleaned.append(e)
    events = cleaned

    matched = [e for e in events if e.matched]
    unmatched = [e for e in events if not e.matched]

    # Sort matched events by best ROI descending
    matched.sort(key=lambda e: _compute_best_roi(e), reverse=True)

    if min_roi is not None:
        matched = [e for e in matched if _compute_best_roi(e) >= min_roi]
        unmatched = []  # Hide unmatched when filtering by ROI

    return matched + unmatched


def _parse_opp_details(opp: dict) -> dict:
    """Ensure opp['details'] is a parsed dict, not a JSON string."""
    details = opp.get("details")
    if isinstance(details, str):
        try:
            opp["details"] = json.loads(details)
        except (json.JSONDecodeError, TypeError):
            opp["details"] = {}
    elif not isinstance(details, dict):
        opp["details"] = {}
    return opp


def _dedupe_opportunities(opportunities: list[dict]) -> list[dict]:
    """Dedupe opportunities per key. Suspicious ones are kept but sorted to the end."""
    seen: dict[tuple, dict] = {}
    suspicious_seen: dict[tuple, dict] = {}
    for opp in opportunities:
        _parse_opp_details(opp)
        details = opp["details"]
        key = (opp.get("team_a", ""), opp.get("platform_buy_yes", ""), opp.get("platform_buy_no", ""))

        if details.get("suspicious"):
            if key not in suspicious_seen:
                suspicious_seen[key] = opp
        else:
            if key not in seen:
                seen[key] = opp

    # Normal opps first, suspicious at the end (exclude suspicious that already have a clean version)
    result = list(seen.values())
    for key, opp in suspicious_seen.items():
        if key not in seen:
            result.append(opp)
    return result


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    opportunities = await db.get_active_opportunities(limit=50)
    opportunities = _dedupe_opportunities(opportunities)
    from src.state import app_state
    events = app_state.get("matched_events", [])
    sport = request.query_params.get("sport", "")
    min_roi_param = request.query_params.get("min_roi", "-5")
    try:
        min_roi = float(min_roi_param) if min_roi_param != "all" else None
    except ValueError:
        min_roi = -5.0
    events = _filter_and_sort_events(events, sport, min_roi=min_roi)

    # Collect available sports for filter buttons
    all_events = app_state.get("matched_events", [])
    sports_set: set[str] = set()
    for e in all_events:
        s = _get_event_sport(e)
        if s:
            sports_set.add(s)
    available_sports = sorted(sports_set)

    # Build set of team_a names that have active arb opportunities
    arb_teams: set[str] = set()
    for opp in opportunities:
        arb_teams.add(opp.get("team_a", ""))

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "opportunities": opportunities,
            "events": events,
            "last_update": datetime.now(UTC).strftime("%H:%M:%S UTC"),
            "current_sport": sport,
            "current_min_roi": min_roi_param,
            "available_sports": available_sports,
            "arb_teams": arb_teams,
            "poly_count": app_state.get("poly_count", 0),
            "kalshi_count": app_state.get("kalshi_count", 0),
            "last_scan_duration": app_state.get("last_scan_duration", 0),
        },
    )


@router.get("/api/opportunities")
async def api_opportunities():
    return await db.get_active_opportunities(limit=50)


@router.get("/api/history")
async def api_history():
    return await db.get_all_opportunities(limit=200)


@router.get("/api/events")
async def api_events():
    from src.state import app_state
    events = app_state.get("matched_events", [])
    return {
        "count": len(events),
        "events": [
            {
                "title": e.title,
                "team_a": e.team_a,
                "team_b": e.team_b,
                "matched": e.matched,
                "platforms": list(e.markets.keys()),
            }
            for e in events
        ],
    }


@router.get("/partials/events", response_class=HTMLResponse)
async def partial_events(request: Request):
    from src.state import app_state
    events = app_state.get("matched_events", [])
    sport = request.query_params.get("sport", "")
    min_roi_param = request.query_params.get("min_roi", "-5")
    try:
        min_roi = float(min_roi_param) if min_roi_param != "all" else None
    except ValueError:
        min_roi = -5.0
    events = _filter_and_sort_events(events, sport, min_roi=min_roi)

    # Build arb_teams set for the green dot indicator
    opportunities = await db.get_active_opportunities(limit=50)
    opportunities = _dedupe_opportunities(opportunities)
    arb_teams: set[str] = set()
    for opp in opportunities:
        arb_teams.add(opp.get("team_a", ""))

    return templates.TemplateResponse(
        "partials/events.html",
        {"request": request, "events": events, "arb_teams": arb_teams},
    )


@router.get("/partials/alerts", response_class=HTMLResponse)
async def partial_alerts(request: Request):
    opportunities = await db.get_active_opportunities(limit=50)
    opportunities = _dedupe_opportunities(opportunities)
    return templates.TemplateResponse(
        "partials/alerts.html",
        {"request": request, "opportunities": opportunities},
    )


@router.get("/api/stream")
async def sse_stream(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _sse_subscribers.append(queue)

    async def event_generator() -> AsyncGenerator[dict, None]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                    yield {"event": "update", "data": msg}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "keepalive"}
        finally:
            _sse_subscribers.remove(queue)

    return EventSourceResponse(event_generator())

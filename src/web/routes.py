from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
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


def _filter_and_sort_events(events: list, sport: str = "") -> list:
    """Filter events by sport and sort: matched by ROI desc, then Kalshi-only."""
    if sport:
        events = [e for e in events if _get_event_sport(e) == sport]

    matched = [e for e in events if e.matched]
    unmatched = [e for e in events if not e.matched]

    # Sort matched events by best ROI descending
    matched.sort(key=lambda e: _compute_best_roi(e), reverse=True)

    return matched + unmatched


def _dedupe_opportunities(opportunities: list[dict]) -> list[dict]:
    """Keep only the latest non-suspicious opportunity per (team_a, platform_buy_yes, platform_buy_no) key."""
    seen: dict[tuple, dict] = {}
    for opp in opportunities:
        # Filter out suspicious opportunities
        details = opp.get("details")
        if isinstance(details, dict) and details.get("suspicious"):
            continue
        if isinstance(details, str):
            try:
                d = json.loads(details)
                if d.get("suspicious"):
                    continue
            except (json.JSONDecodeError, TypeError):
                pass

        key = (opp.get("team_a", ""), opp.get("platform_buy_yes", ""), opp.get("platform_buy_no", ""))
        if key not in seen:
            seen[key] = opp
    return list(seen.values())


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    opportunities = await db.get_active_opportunities(limit=50)
    opportunities = _dedupe_opportunities(opportunities)
    from src.state import app_state
    events = app_state.get("matched_events", [])
    sport = request.query_params.get("sport", "")
    events = _filter_and_sort_events(events, sport)

    # Collect available sports for filter buttons
    all_events = app_state.get("matched_events", [])
    sports_set: set[str] = set()
    for e in all_events:
        s = _get_event_sport(e)
        if s:
            sports_set.add(s)
    available_sports = sorted(sports_set)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "opportunities": opportunities,
            "events": events,
            "last_update": datetime.now(UTC).strftime("%H:%M:%S UTC"),
            "current_sport": sport,
            "available_sports": available_sports,
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
    events = _filter_and_sort_events(events, sport)
    return templates.TemplateResponse(
        "partials/events.html",
        {"request": request, "events": events},
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

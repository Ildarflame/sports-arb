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


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    opportunities = await db.get_active_opportunities(limit=50)
    # Import here to avoid circular import; state is set by main.py
    from src.state import app_state
    events = app_state.get("matched_events", [])
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "opportunities": opportunities,
            "events": events,
            "last_update": datetime.now(UTC).strftime("%H:%M:%S UTC"),
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
    return templates.TemplateResponse(
        "partials/events.html",
        {"request": request, "events": events},
    )


@router.get("/partials/alerts", response_class=HTMLResponse)
async def partial_alerts(request: Request):
    opportunities = await db.get_active_opportunities(limit=50)
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

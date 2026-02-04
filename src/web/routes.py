from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import AsyncGenerator

from fastapi import APIRouter, Request, WebSocket
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
    # 50/50 with no bid/ask = placeholder even with volume
    return (price.yes_price == 0.5
            and price.no_price == 0.5
            and price.yes_bid is None
            and price.yes_ask is None)


def _is_stale_event(event) -> bool:
    """Check if a game event's date is in the past."""
    pm = event.markets.get(Platform.POLYMARKET)
    km = event.markets.get(Platform.KALSHI)
    game_date = (pm.game_date if pm else None) or (km.game_date if km else None)
    if game_date and game_date < date.today():
        return True
    return False


def _is_futures_event(event) -> bool:
    """Check if event is a futures market."""
    pm = event.markets.get(Platform.POLYMARKET)
    km = event.markets.get(Platform.KALSHI)
    pm_type = pm.market_type if pm else ""
    km_type = km.market_type if km else ""
    return pm_type == "futures" or km_type == "futures"


def _filter_and_sort_events(events: list, sport: str = "", min_roi: float | None = None, hide_futures: bool = False) -> list:
    """Filter events by sport/min_roi and sort: matched by ROI desc, then Kalshi-only."""
    if sport:
        events = [e for e in events if _get_event_sport(e) == sport]

    if hide_futures:
        events = [e for e in events if not _is_futures_event(e)]

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
        # Skip extreme prices (<=2c or >=98c) — illiquid or resolved markets
        if km and km.price and (km.price.yes_price <= 0.02 or km.price.yes_price >= 0.98):
            continue
        if pm and pm.price and (pm.price.yes_price <= 0.02 or pm.price.yes_price >= 0.98):
            continue
        # Skip futures with Poly at exactly 50/50 — default/broken pricing
        if (pm and pm.price and pm.price.yes_price == 0.5 and pm.price.no_price == 0.5
                and not e.team_b):
            continue
        # Skip events where BOTH platforms show exactly 50/50 — bad data
        if (pm and pm.price and km and km.price
                and pm.price.yes_price == 0.5 and pm.price.no_price == 0.5
                and km.price.yes_price == 0.5 and km.price.no_price == 0.5):
            continue
        cleaned.append(e)
    events = cleaned

    matched = [e for e in events if e.matched]
    unmatched = [e for e in events if not e.matched]

    # Sort matched events: games first (by ROI desc), then futures (by ROI desc)
    matched_games = [e for e in matched if not _is_futures_event(e)]
    matched_futures = [e for e in matched if _is_futures_event(e)]
    matched_games.sort(key=lambda e: _compute_best_roi(e), reverse=True)
    matched_futures.sort(key=lambda e: _compute_best_roi(e), reverse=True)

    if min_roi is not None:
        matched_games = [e for e in matched_games if _compute_best_roi(e) >= min_roi]
        matched_futures = [e for e in matched_futures if _compute_best_roi(e) >= min_roi]
        unmatched = []  # Hide unmatched when filtering by ROI

    return matched_games + matched_futures + unmatched


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


_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def _sort_by_confidence_then_roi(opportunities: list[dict]) -> list[dict]:
    """Sort opportunities: HIGH confidence first, then by ROI desc within each tier."""
    def _key(opp):
        details = opp.get("details", {})
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except (json.JSONDecodeError, TypeError):
                details = {}
        conf = details.get("confidence", "low")
        tier = _CONFIDENCE_ORDER.get(conf, 2)
        roi = opp.get("roi_after_fees", 0) or 0
        return (tier, -roi)
    return sorted(opportunities, key=_key)


def _dedupe_opportunities(opportunities: list[dict]) -> list[dict]:
    """Dedupe opportunities per game. Both team perspectives of the same game collapse into one."""
    seen: dict[tuple, dict] = {}
    suspicious_seen: dict[tuple, dict] = {}
    for opp in opportunities:
        _parse_opp_details(opp)
        details = opp["details"]
        # Use sorted team names so both perspectives of the same game produce the same key
        teams = tuple(sorted([opp.get("team_a", "").lower().strip(),
                              opp.get("team_b", "").lower().strip()]))
        key = teams

        if details.get("suspicious"):
            if key not in suspicious_seen:
                suspicious_seen[key] = opp
        else:
            # Keep the one with higher ROI
            if key not in seen or (opp.get("roi_after_fees", 0) or 0) > (seen[key].get("roi_after_fees", 0) or 0):
                seen[key] = opp

    # Normal opps first, suspicious at the end (exclude suspicious that already have a clean version)
    result = list(seen.values())
    for key, opp in suspicious_seen.items():
        if key not in seen:
            result.append(opp)
    return _sort_by_confidence_then_roi(result)


def _filter_by_confidence(opportunities: list[dict], min_confidence: str) -> list[dict]:
    """Filter opportunities by minimum confidence tier."""
    if not min_confidence or min_confidence == "all":
        return opportunities
    allowed = set()
    if min_confidence == "high":
        allowed = {"high"}
    elif min_confidence == "medium":
        allowed = {"high", "medium"}
    elif min_confidence == "low":
        allowed = {"high", "medium", "low"}
    else:
        return opportunities
    result = []
    for opp in opportunities:
        details = opp.get("details", {})
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except (json.JSONDecodeError, TypeError):
                details = {}
        conf = details.get("confidence", "low")
        if conf in allowed:
            result.append(opp)
    return result


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    opportunities = await db.get_active_opportunities(limit=50)
    opportunities = _dedupe_opportunities(opportunities)
    min_confidence = request.query_params.get("min_confidence", "all")
    opportunities = _filter_by_confidence(opportunities, min_confidence)
    from src.state import app_state
    events = app_state.get("matched_events", [])
    sport = request.query_params.get("sport", "")
    min_roi_param = request.query_params.get("min_roi", "-5")
    hide_futures = request.query_params.get("hide_futures", "") == "1"
    try:
        min_roi = float(min_roi_param) if min_roi_param != "all" else None
    except ValueError:
        min_roi = -5.0
    events = _filter_and_sort_events(events, sport, min_roi=min_roi, hide_futures=hide_futures)

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
    hide_futures = request.query_params.get("hide_futures", "") == "1"
    try:
        min_roi = float(min_roi_param) if min_roi_param != "all" else None
    except ValueError:
        min_roi = -5.0
    events = _filter_and_sort_events(events, sport, min_roi=min_roi, hide_futures=hide_futures)

    # Build arb_teams set for the green dot indicator
    opportunities = await db.get_active_opportunities(limit=50)
    opportunities = _dedupe_opportunities(opportunities)
    arb_teams: set[str] = set()
    for opp in opportunities:
        arb_teams.add(opp.get("team_a", ""))

    return templates.TemplateResponse(
        "partials/events.html",
        {"request": request, "events": events, "arb_teams": arb_teams,
         "scan_metrics_by_sport": app_state.get("scan_metrics_by_sport", {})},
    )


@router.get("/partials/alerts", response_class=HTMLResponse)
async def partial_alerts(request: Request):
    opportunities = await db.get_active_opportunities(limit=50)
    opportunities = _dedupe_opportunities(opportunities)
    return templates.TemplateResponse(
        "partials/alerts.html",
        {"request": request, "opportunities": opportunities},
    )


@router.get("/api/calculate")
async def api_calculate(request: Request):
    """Calculate bet sizes for an opportunity."""
    from src.engine.arbitrage import calculate_bet_sizes
    opp_id = request.query_params.get("opp_id", "")
    try:
        bankroll = float(request.query_params.get("bankroll", "100"))
    except ValueError:
        bankroll = 100.0

    if not opp_id:
        return {"error": "opp_id required"}

    opps = await db.get_active_opportunities(limit=100)
    opp = None
    for o in opps:
        if o.get("id") == opp_id:
            opp = o
            break
    if not opp:
        return {"error": "opportunity not found"}

    yes_plat = Platform(opp["platform_buy_yes"])
    no_plat = Platform(opp["platform_buy_no"])

    result = calculate_bet_sizes(
        yes_price=opp["yes_price"],
        no_price=opp["no_price"],
        yes_platform=yes_plat,
        no_platform=no_plat,
        bankroll=bankroll,
    )
    if result is None:
        return {"error": "Invalid prices - no arbitrage opportunity"}
    result["opp_id"] = opp_id
    result["bankroll"] = bankroll
    return result


@router.get("/api/roi-history/{opp_id}")
async def api_roi_history(opp_id: str):
    """Return ROI time series for an opportunity."""
    history = await db.get_roi_history(opp_id)
    return history


@router.get("/partials/roi-history/{opp_id}", response_class=HTMLResponse)
async def partial_roi_history(opp_id: str):
    """Return a formatted ROI sparkline for an opportunity."""
    history = await db.get_roi_history(opp_id)
    if not history:
        return HTMLResponse('<span style="color:var(--text-dim);font-size:0.7rem;">No ROI data yet</span>')
    # Show last 10 snapshots as text sparkline
    recent = history[-10:]
    parts = [f'{h["roi"]:.1f}%' for h in recent]
    trend = " → ".join(parts)
    return HTMLResponse(
        f'<div style="display:block;font-size:0.7rem;color:var(--text-dim);padding:0.3rem 0;">'
        f'ROI trend: {trend}</div>'
    )


@router.get("/api/analytics")
async def api_analytics():
    return await db.get_analytics()


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    data = await db.get_analytics()
    return templates.TemplateResponse(
        "analytics.html",
        {"request": request, "data": data, "last_update": datetime.now(UTC).strftime("%H:%M:%S UTC")},
    )


@router.get("/api/simulate")
async def api_simulate(request: Request):
    """Run P&L simulation."""
    try:
        bankroll = float(request.query_params.get("bankroll", "1000"))
    except ValueError:
        bankroll = 1000.0
    try:
        min_roi = float(request.query_params.get("min_roi", "1"))
    except ValueError:
        min_roi = 1.0
    min_confidence = request.query_params.get("min_confidence", "low")
    try:
        days = int(request.query_params.get("days", "30"))
    except ValueError:
        days = 30
    return await db.simulate_pnl(bankroll, min_roi, min_confidence, days)


@router.get("/simulator", response_class=HTMLResponse)
async def simulator_page(request: Request):
    try:
        bankroll = float(request.query_params.get("bankroll", "1000"))
    except ValueError:
        bankroll = 1000.0
    try:
        min_roi = float(request.query_params.get("min_roi", "1"))
    except ValueError:
        min_roi = 1.0
    min_confidence = request.query_params.get("min_confidence", "low")
    try:
        days = int(request.query_params.get("days", "30"))
    except ValueError:
        days = 30
    data = await db.simulate_pnl(bankroll, min_roi, min_confidence, days)
    return templates.TemplateResponse(
        "simulator.html",
        {
            "request": request,
            "data": data,
            "last_update": datetime.now(UTC).strftime("%H:%M:%S UTC"),
        },
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


# --- Executor Dashboard ---

# Global executor components (initialized in main.py)
_executor_ws_handler = None


def set_executor_ws_handler(handler) -> None:
    """Set the WebSocket handler from main.py initialization."""
    global _executor_ws_handler
    _executor_ws_handler = handler


@router.get("/executor", response_class=HTMLResponse)
async def executor_page(request: Request):
    """Executor dashboard page."""
    from src.state import app_state

    # Get settings manager from app state
    settings_manager = app_state.get("executor_settings_manager")
    if settings_manager:
        settings = settings_manager.get()
    else:
        # Fallback defaults
        from src.executor import ExecutorSettings
        settings = ExecutorSettings()

    # Get balances
    balances = {"poly": 0.0, "kalshi": 0.0}
    poly_connector = app_state.get("poly_connector")
    kalshi_connector = app_state.get("kalshi_connector")
    try:
        if poly_connector:
            balances["poly"] = await poly_connector.get_balance()
        if kalshi_connector:
            balances["kalshi"] = await kalshi_connector.get_balance()
    except Exception as e:
        logger.warning(f"Failed to fetch balances for executor page: {e}")

    # Get stats and positions
    stats = await db.get_daily_executor_stats()
    positions = await db.get_executor_positions(status="open")
    trades = await db.get_executor_trades(limit=20)

    return templates.TemplateResponse(
        "executor.html",
        {
            "request": request,
            "enabled": settings.enabled,
            "settings": settings,
            "balances": balances,
            "stats": stats,
            "positions": positions,
            "trades": trades,
        },
    )


@router.websocket("/ws/executor")
async def executor_websocket(websocket: WebSocket):
    """WebSocket endpoint for executor real-time updates."""
    if _executor_ws_handler is None:
        await websocket.close(code=1011, reason="Executor not initialized")
        return

    await _executor_ws_handler.handle_connection(websocket)

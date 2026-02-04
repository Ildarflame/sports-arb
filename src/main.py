from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import date, timedelta

import uvicorn

from src.config import settings
from src.connectors.kalshi import KalshiConnector
from src.connectors.polymarket import PolymarketConnector
from src.db import db
from src.engine.arbitrage import calculate_arbitrage, calculate_3way_arbitrage
from src.engine.matcher import match_events, find_3way_groups
from src.models import ArbitrageOpportunity, MarketPrice, Platform, SportEvent, ThreeWayGroup

# Executor imports (conditional to avoid breaking if not configured)
_executor = None  # Global executor instance
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

from src.state import app_state

# Cache refresh intervals (seconds)
KALSHI_CACHE_TTL = 600  # 10 minutes
POLY_CACHE_TTL = 300    # 5 minutes
MATCH_CACHE_TTL = 300   # 5 minutes for matched events cache

# Concurrency limiter for price fetches (increased from 15 for faster book fetches)
_price_semaphore = asyncio.Semaphore(25)

# Token → event mapping for O(1) WS price application
_token_to_event: dict[str, SportEvent] = {}


async def _fetch_with_semaphore(coro, timeout: float = 5.0):
    """Run a coroutine with semaphore-limited concurrency and timeout."""
    async with _price_semaphore:
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.debug("Price fetch timed out after %.1fs", timeout)
            return None


def _is_valid_price(price: MarketPrice | None) -> bool:
    """Check if a price looks real (not a 50/50 default placeholder)."""
    if not price:
        return False
    # Exactly 0.5/0.5 with no bid/ask = placeholder (even with volume)
    if (price.yes_price == 0.5
            and price.no_price == 0.5
            and price.yes_bid is None
            and price.yes_ask is None):
        return False
    return True


def _is_stale_event(event: SportEvent) -> bool:
    """Check if a game event's date is in the past."""
    pm = event.markets.get(Platform.POLYMARKET)
    km = event.markets.get(Platform.KALSHI)
    # Use earliest available game_date from either platform
    dates = [m.game_date for m in (pm, km) if m and m.game_date]
    if dates and max(dates) < date.today():
        return True
    return False


async def fetch_and_update_prices(
    poly: PolymarketConnector,
    kalshi: KalshiConnector,
    events: list[SportEvent],
) -> None:
    """Fetch latest prices for all matched events using batch/parallel fetching."""
    # Collect all price fetch tasks across all events
    # NOTE: Kalshi prices are already set during fetch_sports_events() — no re-fetch needed.
    poly_normal_tokens: list[tuple[SportEvent, str]] = []  # (event, token_id)
    poly_neg_risk_ids: list[tuple[SportEvent, str]] = []   # (event, market_id)

    for event in events:
        pm = event.markets.get(Platform.POLYMARKET)

        if pm:
            token_ids = pm.raw_data.get("clob_token_ids", [])
            price_id = token_ids[0] if token_ids else pm.market_id
            is_neg_risk = pm.raw_data.get("neg_risk", False)
            if is_neg_risk:
                poly_neg_risk_ids.append((event, price_id))
            else:
                poly_normal_tokens.append((event, price_id))

    # Batch fetch: Polymarket normal tokens via batch API
    batch_prices = {}
    if poly_normal_tokens:
        all_token_ids = [tid for _, tid in poly_normal_tokens]
        try:
            batch_prices = await poly.fetch_prices_batch(all_token_ids)
            logger.info(f"Batch fetched {len(batch_prices)}/{len(all_token_ids)} Poly prices")
        except Exception:
            logger.exception("Batch price fetch failed, falling back to individual")

    # Apply batch results to events (preserve volume from Gamma)
    for event, token_id in poly_normal_tokens:
        pm = event.markets.get(Platform.POLYMARKET)
        if pm and token_id in batch_prices:
            new_price = batch_prices[token_id]
            # Preserve volume from Gamma API since CLOB /prices doesn't return volume
            if pm.price and pm.price.volume:
                new_price.volume = pm.price.volume
            pm.price = new_price

    # Individual fetches for negRisk Poly markets + missing batch results + Kalshi
    individual_tasks: list[tuple[str, SportEvent, str, asyncio.Task]] = []

    for event, market_id in poly_neg_risk_ids:
        pm = event.markets.get(Platform.POLYMARKET)
        clob_ids = pm.raw_data.get("clob_token_ids", []) if pm else []
        clob_token_id = clob_ids[0] if clob_ids else None
        task = asyncio.ensure_future(
            _fetch_with_semaphore(
                poly.fetch_price(market_id, neg_risk=True, clob_token_id=clob_token_id)
            )
        )
        individual_tasks.append(("poly", event, market_id, task))

    # Fetch any Poly tokens that weren't in the batch result
    for event, token_id in poly_normal_tokens:
        pm = event.markets.get(Platform.POLYMARKET)
        if pm and pm.price is None and token_id not in batch_prices:
            task = asyncio.ensure_future(
                _fetch_with_semaphore(poly.fetch_price(token_id, neg_risk=False))
            )
            individual_tasks.append(("poly", event, token_id, task))

    if individual_tasks:
        results = await asyncio.gather(
            *[t[3] for t in individual_tasks], return_exceptions=True
        )
        for (label, event, mid, _), result in zip(individual_tasks, results):
            if isinstance(result, Exception):
                logger.warning(f"Price fetch error ({label}, {mid[:20]}): {result}")
                continue
            if result is None:
                continue
            if label == "poly":
                pm = event.markets.get(Platform.POLYMARKET)
                if pm:
                    pm.price = result

        logger.info(f"Individual price fetches: {len(individual_tasks)} (negRisk + batch fallback)")


async def fetch_books_for_candidates(
    poly: PolymarketConnector,
    kalshi: KalshiConnector,
    candidates: list[SportEvent],
) -> None:
    """Fetch order books for arb candidates: Polymarket books + fresh Kalshi prices."""
    poly_tasks: list[tuple[SportEvent, asyncio.Task]] = []
    kalshi_tasks: list[tuple[SportEvent, asyncio.Task]] = []

    for event in candidates:
        pm = event.markets.get(Platform.POLYMARKET)
        if pm:
            token_ids = pm.raw_data.get("clob_token_ids", [])
            token_id = token_ids[0] if token_ids else pm.market_id
            task = asyncio.ensure_future(
                _fetch_with_semaphore(poly.fetch_book(token_id))
            )
            poly_tasks.append((event, task))

        km = event.markets.get(Platform.KALSHI)
        if km:
            task = asyncio.ensure_future(
                _fetch_with_semaphore(kalshi.fetch_price(km.market_id))
            )
            kalshi_tasks.append((event, task))

    all_tasks = [t[1] for t in poly_tasks] + [t[1] for t in kalshi_tasks]
    if not all_tasks:
        return

    results = await asyncio.gather(*all_tasks, return_exceptions=True)

    # Apply Polymarket book results
    poly_updated = 0
    for i, (event, _) in enumerate(poly_tasks):
        result = results[i]
        if isinstance(result, Exception) or result is None:
            continue
        pm = event.markets.get(Platform.POLYMARKET)
        if pm and pm.price:
            result.volume = pm.price.volume
            pm.price = result
            poly_updated += 1
        elif pm:
            pm.price = result
            poly_updated += 1

    # Apply Kalshi fresh price results
    kalshi_updated = 0
    offset = len(poly_tasks)
    for i, (event, _) in enumerate(kalshi_tasks):
        result = results[offset + i]
        if isinstance(result, Exception) or result is None:
            continue
        km = event.markets.get(Platform.KALSHI)
        if km:
            km.price = result
            kalshi_updated += 1

    # Diagnostic: count candidates with real bid/ask data
    poly_exec = 0
    kalshi_exec = 0
    for event, _ in poly_tasks:
        pm = event.markets.get(Platform.POLYMARKET)
        if pm and pm.price and pm.price.yes_bid is not None and pm.price.yes_ask is not None:
            poly_exec += 1
    for event, _ in kalshi_tasks:
        km = event.markets.get(Platform.KALSHI)
        if km and km.price and km.price.yes_bid is not None and km.price.yes_ask is not None:
            kalshi_exec += 1

    total = len(poly_tasks) or 1
    logger.info(
        f"Book fetch: {poly_updated}/{len(poly_tasks)} Poly books, "
        f"{kalshi_updated}/{len(kalshi_tasks)} Kalshi prices refreshed | "
        f"EXEC data: {poly_exec}/{total} Poly bid/ask, {kalshi_exec}/{total} Kalshi bid/ask"
    )


def broadcast_event(event_type: str, data: dict) -> None:
    from src.web.routes import broadcast_event as _broadcast
    _broadcast(event_type, data)


async def ws_price_listener(poly: PolymarketConnector) -> None:
    """Background task: consume Polymarket WS price stream and update cache.

    Reconnects when the subscription set changes (new matched events) or on
    connection errors. The 5-min full refetch remains as fallback — WS is an
    acceleration layer, not a replacement.
    """
    _last_sub_snapshot: set[str] = set()

    while app_state["running"]:
        subscribed = app_state["ws_subscribed_ids"]
        if not subscribed:
            await asyncio.sleep(2)
            continue
        try:
            token_list = list(subscribed)
            _last_sub_snapshot = set(subscribed)
            logger.info(f"Polymarket WS: subscribing to {len(token_list)} tokens")
            async for token_id, price in poly.subscribe_prices(token_list):
                if not app_state["running"]:
                    break
                app_state["ws_price_cache"][token_id] = price
                app_state["ws_update_count"] += 1
                # Live-update the event if mapped
                event = _token_to_event.get(token_id)
                if event:
                    pm = event.markets.get(Platform.POLYMARKET)
                    if pm and pm.price:
                        # Preserve bid/ask from book fetch, only update midpoint
                        pm.price.yes_price = price.yes_price
                        pm.price.no_price = price.no_price
                        pm.price.last_updated = price.last_updated
                    elif pm:
                        pm.price = price
                # Check if subscription set changed — reconnect to pick up new tokens
                if app_state["ws_subscribed_ids"] != _last_sub_snapshot:
                    logger.info("WS subscription set changed, reconnecting...")
                    break
        except Exception:
            logger.exception("WS price listener error, restarting...")
            await asyncio.sleep(5)


def _update_ws_subscriptions(events: list[SportEvent]) -> None:
    """Refresh WS subscription set and token→event mapping from matched events."""
    global _token_to_event
    new_map: dict[str, SportEvent] = {}
    new_ids: set[str] = set()
    for event in events:
        pm = event.markets.get(Platform.POLYMARKET)
        if not pm:
            continue
        token_ids = pm.raw_data.get("clob_token_ids", [])
        if token_ids:
            tid = token_ids[0]
            new_ids.add(tid)
            new_map[tid] = event
    old_count = len(app_state["ws_subscribed_ids"])
    # Atomic swap: single assignment replaces the entire dict
    _token_to_event = new_map
    app_state["ws_subscribed_ids"] = new_ids
    if len(new_ids) != old_count:
        logger.info(f"WS subscriptions updated: {old_count} → {len(new_ids)} tokens")


def _apply_ws_cache(events: list[SportEvent]) -> int:
    """Apply cached WS prices to events. WS prices are fresher than Gamma.

    Updates midpoint even if event already has a price (overrides stale Gamma).
    Preserves bid/ask from any prior book fetch.
    """
    ws_cache = app_state["ws_price_cache"]
    if not ws_cache:
        return 0
    updated = 0
    for event in events:
        pm = event.markets.get(Platform.POLYMARKET)
        if not pm:
            continue
        token_ids = pm.raw_data.get("clob_token_ids", [])
        if not token_ids or token_ids[0] not in ws_cache:
            continue
        ws_price = ws_cache[token_ids[0]]
        if pm.price and (pm.price.yes_bid is not None or pm.price.yes_ask is not None):
            # Has book data — only update midpoint, keep bid/ask
            pm.price.yes_price = ws_price.yes_price
            pm.price.no_price = ws_price.no_price
            pm.price.last_updated = ws_price.last_updated
        else:
            # No book data — use full WS price, preserve volume
            vol = pm.price.volume if pm.price and pm.price.volume else 0
            pm.price = ws_price
            if vol:
                pm.price.volume = vol
        updated += 1
    return updated


def _get_event_sport(event: SportEvent) -> str:
    """Extract sport from a SportEvent's markets."""
    pm = event.markets.get(Platform.POLYMARKET)
    km = event.markets.get(Platform.KALSHI)
    return (pm.sport if pm else "") or (km.sport if km else "") or "other"


async def _process_sport_group(
    sport: str,
    events: list[SportEvent],
    poly: PolymarketConnector,
    kalshi: KalshiConnector,
) -> tuple[list[tuple[SportEvent, ArbitrageOpportunity]], float]:
    """Process arb screening + book fetching + calculation for a sport group.

    Returns (list of (event, opportunity) pairs, duration in seconds).
    """
    start = time.monotonic()
    results: list[tuple[SportEvent, ArbitrageOpportunity]] = []

    # Screen candidates by midpoint cost
    arb_candidates: list[SportEvent] = []
    for event in events:
        if _is_stale_event(event):
            continue
        pm = event.markets.get(Platform.POLYMARKET)
        km = event.markets.get(Platform.KALSHI)
        if not (pm and km and pm.price and km.price):
            continue
        pp, kp = pm.price, km.price
        if not _is_valid_price(pp) or not _is_valid_price(kp):
            continue
        if kp.yes_price <= 0.02 or kp.yes_price >= 0.98:
            continue
        if pp.yes_price <= 0.02 or pp.yes_price >= 0.98:
            continue
        if (pp.volume or 0) == 0 or (kp.volume or 0) == 0:
            continue
        combined_vol = (pp.volume or 0) + (kp.volume or 0)
        if combined_vol < 100:
            continue
        if pp.yes_price == 0.5 and pp.no_price == 0.5 and not event.team_b:
            continue
        if event.teams_swapped:
            cost1 = pp.yes_price + (1 - kp.yes_price)
            cost2 = kp.no_price + pp.no_price
        else:
            cost1 = pp.yes_price + kp.no_price
            cost2 = kp.yes_price + pp.no_price
        if cost1 < 1.02 or cost2 < 1.02:
            arb_candidates.append(event)

    # Fetch order books for candidates
    if arb_candidates:
        await fetch_books_for_candidates(poly, kalshi, arb_candidates)

    # Calculate arbitrage (only for non-stale events)
    # Second-chance pass: if arb found but no bid/ask, fetch books and recalculate
    needs_book: list[tuple[SportEvent, ArbitrageOpportunity]] = []
    for event in events:
        if _is_stale_event(event):
            continue
        opp = calculate_arbitrage(event)
        if opp:
            pm = event.markets.get(Platform.POLYMARKET)
            has_bid_ask = pm and pm.price and pm.price.yes_bid is not None
            if has_bid_ask:
                results.append((event, opp))
            else:
                needs_book.append((event, opp))

    if needs_book:
        book_events = [ev for ev, _ in needs_book]
        logger.info(f"Second-chance book fetch for {len(book_events)} arb events without bid/ask")
        await fetch_books_for_candidates(poly, kalshi, book_events)
        for event, midpoint_opp in needs_book:
            opp = calculate_arbitrage(event)
            if opp:
                # Recalc produced arb with real bid/ask — use it
                results.append((event, opp))
            else:
                # Arb disappeared at real prices — keep midpoint arb but
                # enrich with bid/ask data so dashboard shows market info
                pm = event.markets.get(Platform.POLYMARKET)
                km = event.markets.get(Platform.KALSHI)
                if pm and pm.price:
                    midpoint_opp.details["poly_yes_bid"] = pm.price.yes_bid
                    midpoint_opp.details["poly_yes_ask"] = pm.price.yes_ask
                    midpoint_opp.details["has_poly_exec"] = bool(
                        pm.price.yes_bid and pm.price.yes_ask
                    )
                if km and km.price:
                    midpoint_opp.details["kalshi_yes_bid"] = km.price.yes_bid
                    midpoint_opp.details["kalshi_yes_ask"] = km.price.yes_ask
                    midpoint_opp.details["has_kalshi_exec"] = bool(
                        km.price.yes_bid and km.price.yes_ask
                    )
                midpoint_opp.details["midpoint_only"] = True
                midpoint_opp.details["confidence"] = "low"
                results.append((event, midpoint_opp))

    duration = time.monotonic() - start
    return results, duration


async def scan_loop(poly: PolymarketConnector, kalshi: KalshiConnector) -> None:
    """Main scanning loop: fetch events, match, check arbitrage."""
    _scan_count = 0
    while app_state["running"]:
        try:
            scan_start = time.monotonic()
            logger.info("--- Scanning for events ---")

            now = time.monotonic()

            # Caching: reuse Kalshi markets if cache is fresh
            kalshi_cache_age = now - app_state["kalshi_cache_time"]
            poly_cache_age = now - app_state["poly_cache_time"]

            fetch_poly = poly_cache_age >= POLY_CACHE_TTL or not app_state["poly_cache"]
            fetch_kalshi = kalshi_cache_age >= KALSHI_CACHE_TTL or not app_state["kalshi_cache"]

            if fetch_poly and fetch_kalshi:
                poly_markets, kalshi_markets = await asyncio.gather(
                    poly.fetch_sports_events(),
                    kalshi.fetch_sports_events(),
                )
                app_state["poly_cache"] = poly_markets
                app_state["poly_cache_time"] = now
                app_state["kalshi_cache"] = kalshi_markets
                app_state["kalshi_cache_time"] = now
            elif fetch_poly:
                poly_markets = await poly.fetch_sports_events()
                app_state["poly_cache"] = poly_markets
                app_state["poly_cache_time"] = now
                kalshi_markets = app_state["kalshi_cache"]
            elif fetch_kalshi:
                kalshi_markets = await kalshi.fetch_sports_events()
                app_state["kalshi_cache"] = kalshi_markets
                app_state["kalshi_cache_time"] = now
                poly_markets = app_state["poly_cache"]
            else:
                poly_markets = app_state["poly_cache"]
                kalshi_markets = app_state["kalshi_cache"]

            # Update metrics
            app_state["poly_count"] = len(poly_markets)
            app_state["kalshi_count"] = len(kalshi_markets)

            logger.info(
                f"Fetched {len(poly_markets)} Polymarket, {len(kalshi_markets)} Kalshi markets"
                f" (cache: poly={'HIT' if not fetch_poly else 'MISS'}, "
                f"kalshi={'HIT' if not fetch_kalshi else 'MISS'})"
            )

            # Match events across platforms (with caching)
            match_cache_age = now - app_state["matched_events_cache_time"]
            use_match_cache = (
                match_cache_age < MATCH_CACHE_TTL
                and app_state["matched_events_cache"]
                and not fetch_poly
                and not fetch_kalshi
            )

            if use_match_cache:
                # Reuse cached matches - much faster than re-running matcher
                matched = list(app_state["matched_events_cache"].values())
                logger.info(f"Using cached matches ({len(matched)} events, {match_cache_age:.0f}s old)")
            else:
                # Full re-match required
                matched = match_events(poly_markets, kalshi_markets)
                # Build cache: (poly_id, kalshi_id) -> SportEvent
                app_state["matched_events_cache"] = {
                    (
                        e.markets.get(Platform.POLYMARKET).market_id if e.markets.get(Platform.POLYMARKET) else "",
                        e.markets.get(Platform.KALSHI).market_id if e.markets.get(Platform.KALSHI) else "",
                    ): e
                    for e in matched
                    if e.matched
                }
                app_state["matched_events_cache_time"] = now
                logger.info(f"Rebuilt match cache ({len(matched)} events)")

            app_state["matched_events"] = matched

            # Update WS subscriptions with current matched token_ids
            _update_ws_subscriptions(matched)

            # Log Kalshi-only games for well-known soccer leagues (debug level)
            _major_soccer_leagues = {"soccer"}
            for event in matched:
                if not event.matched:
                    km = event.markets.get(Platform.KALSHI)
                    if km and km.sport in _major_soccer_leagues and event.team_b:
                        logger.debug(
                            f"Kalshi-only: {event.team_a} vs {event.team_b} ({km.sport}) "
                            f"— no Poly match found"
                        )

            if matched:
                # Pass 0.5: Apply any cached WS prices before full fetch
                ws_applied = _apply_ws_cache(matched)
                if ws_applied:
                    logger.info(f"WS cache: applied {ws_applied} cached prices")

                # Pass 1: Fetch midpoint prices for all matched events
                await fetch_and_update_prices(poly, kalshi, matched)

                # Group events by sport for parallel processing
                from collections import defaultdict as _defaultdict
                sport_groups: dict[str, list[SportEvent]] = _defaultdict(list)
                for event in matched:
                    sport_groups[_get_event_sport(event)].append(event)

                # Pass 2+3: Process each sport group in parallel
                sport_tasks = {
                    sport: _process_sport_group(sport, events, poly, kalshi)
                    for sport, events in sport_groups.items()
                }
                sport_results = await asyncio.gather(
                    *sport_tasks.values(), return_exceptions=True,
                )

                # Pass 4: 3-Way arbitrage for soccer (separate pass using raw markets)
                threeway_results: list[ArbitrageOpportunity] = []
                try:
                    threeway_groups = find_3way_groups(poly_markets, kalshi_markets)
                    for group in threeway_groups:
                        opp = calculate_3way_arbitrage(group)
                        if opp:
                            threeway_results.append(opp)
                    if threeway_results:
                        logger.info(f"3-Way arbitrage: found {len(threeway_results)} opportunities")
                except Exception as e:
                    logger.warning(f"3-Way arbitrage pass failed: {e}")

                # Collect results and per-sport timing
                current_arb_keys: set[tuple[str, str, str]] = set()
                seen_game_keys: set[tuple[str, ...]] = set()
                sport_timings: dict[str, float] = {}

                for sport_name, result in zip(sport_tasks.keys(), sport_results):
                    if isinstance(result, Exception):
                        logger.warning(f"Sport worker {sport_name} failed: {result}")
                        continue

                    arb_results, duration = result
                    sport_timings[sport_name] = round(duration, 2)

                    for event, opp in arb_results:
                        if not (opp.roi_after_fees >= settings.min_arb_percent):
                            continue
                        if opp.roi_after_fees > settings.max_arb_percent:
                            logger.warning(
                                f"SUSPICIOUS ARB (skipped, ROI>{settings.max_arb_percent}%): "
                                f"{opp.event_title} ROI={opp.roi_after_fees}%"
                            )
                            continue

                        game_key = tuple(sorted([
                            opp.team_a.lower().strip(),
                            opp.team_b.lower().strip(),
                        ]))
                        if game_key in seen_game_keys:
                            continue
                        seen_game_keys.add(game_key)

                        arb_key = (
                            opp.team_a,
                            opp.platform_buy_yes.value,
                            opp.platform_buy_no.value,
                        )
                        current_arb_keys.add(arb_key)

                        _pm = event.markets.get(Platform.POLYMARKET)
                        _km = event.markets.get(Platform.KALSHI)
                        _sport = (_pm.sport if _pm else "") or (_km.sport if _km else "")
                        opp_id = await db.save_opportunity(opp, sport=_sport)

                        # Diagnostic: Log spread/O-U and other special market types
                        _subtype = opp.details.get("market_subtype", "moneyline")
                        _line = opp.details.get("line")
                        _arb_type = opp.details.get("arb_type", "yes_no")
                        _is_live = opp.details.get("is_live", False)

                        type_info = ""
                        if _subtype == "spread":
                            type_info = f" [SPREAD {_line}]"
                        elif _subtype == "over_under":
                            type_info = f" [O/U {abs(_line) if _line else ''}]"
                        if _arb_type == "cross_team":
                            type_info += " [CROSS-TEAM]"
                        if _is_live:
                            type_info += " [LIVE]"

                        logger.info(
                            f"ARBITRAGE SAVED: {opp.event_title} "
                            f"ROI={opp.roi_after_fees}% id={opp_id}{type_info}"
                        )
                        broadcast_event("new_arb", {
                            "event": opp.event_title,
                            "roi": opp.roi_after_fees,
                            "cost": opp.total_cost,
                        })

                        # Try to execute if executor is enabled
                        if _executor is not None and opp.roi_after_fees >= settings.executor_min_roi:
                            try:
                                result = await _executor.try_execute(opp)
                                if result:
                                    logger.info(f"EXECUTOR: {opp.event_title} -> {result.status.value}")
                            except Exception as e:
                                logger.error(f"Executor error for {opp.event_title}: {e}")

                # Process 3-way arbitrage results
                for opp in threeway_results:
                    if not (opp.roi_after_fees >= settings.min_arb_percent):
                        continue
                    if opp.roi_after_fees > settings.max_arb_percent:
                        logger.warning(
                            f"SUSPICIOUS 3-WAY ARB (skipped): {opp.event_title} ROI={opp.roi_after_fees}%"
                        )
                        continue

                    game_key = tuple(sorted([
                        opp.team_a.lower().strip(),
                        opp.team_b.lower().strip(),
                    ]))
                    if game_key in seen_game_keys:
                        continue
                    seen_game_keys.add(game_key)

                    arb_key = (
                        opp.team_a,
                        opp.platform_buy_yes.value,
                        "3way",  # Special key for 3-way arbs
                    )
                    current_arb_keys.add(arb_key)

                    opp_id = await db.save_opportunity(opp, sport="soccer")
                    logger.info(
                        f"3-WAY ARBITRAGE SAVED: {opp.event_title} "
                        f"ROI={opp.roi_after_fees}% id={opp_id} [3-WAY]"
                    )
                    broadcast_event("new_arb", {
                        "event": opp.event_title,
                        "roi": opp.roi_after_fees,
                        "cost": opp.total_cost,
                        "type": "3way",
                    })

                # Log per-sport timing
                app_state["scan_metrics_by_sport"] = sport_timings
                if sport_timings:
                    timing_str = ", ".join(f"{s}={t}s" for s, t in sorted(sport_timings.items()))
                    logger.info(f"Sport workers: {timing_str}")

                # Save ROI snapshots for all active arbs
                active_opps = await db.get_active_opportunities(limit=200)
                for active_opp in active_opps:
                    await db.save_roi_snapshot(
                        active_opp["id"], active_opp.get("roi_after_fees", 0)
                    )

                # Deactivate stale opportunities not found this scan
                active_keys = await db.get_active_opp_keys()
                for key, opp_id in active_keys.items():
                    if key not in current_arb_keys:
                        n = await db.deactivate_by_key(*key)
                        logger.info(f"Deactivated stale arbs: {n} entries for key={key}")

                broadcast_event("price_update", {
                    "matched_count": len(matched),
                })

            # Polymarket tag discovery — run once per hour
            tag_discovery_age = time.monotonic() - app_state["last_tag_discovery"]
            if tag_discovery_age >= 3600:  # 60 minutes
                app_state["last_tag_discovery"] = time.monotonic()
                try:
                    new_tags = await poly.discover_sports_tags()
                    if new_tags:
                        logger.info(
                            f"Discovery: {len(new_tags)} new tags added, "
                            f"{len(poly._dynamic_tag_slugs)} total dynamic tags"
                        )
                except Exception:
                    logger.exception("Tag discovery failed")

            # Batch commit all DB writes from this scan cycle
            await db.commit()

            # Periodic cleanup: delete old inactive opportunities every 100 scans
            _scan_count += 1
            if _scan_count % 100 == 0:
                deleted = await db.cleanup_old(days=7)
                snap_deleted = await db.cleanup_old_snapshots(days=7)
                if deleted or snap_deleted:
                    logger.info(
                        f"DB cleanup: removed {deleted} old opps, {snap_deleted} old snapshots"
                    )
                await db.commit()

            scan_duration = time.monotonic() - scan_start
            app_state["last_scan_duration"] = round(scan_duration, 1)
            logger.info(f"Scan completed in {scan_duration:.1f}s")

        except Exception:
            logger.exception("Error in scan loop")

        await asyncio.sleep(settings.poll_interval)


async def kalshi_price_poller(kalshi: KalshiConnector) -> None:
    """Background task: poll fresh Kalshi prices for markets in active arbs."""
    while app_state["running"]:
        try:
            await asyncio.sleep(30)  # Poll every 30 seconds
            if not app_state["running"]:
                break

            # Get active opportunities to find Kalshi market IDs
            active_opps = await db.get_active_opportunities(limit=100)
            if not active_opps:
                continue

            # Collect Kalshi market IDs from matched events that have active arbs
            arb_teams = {opp.get("team_a", "") for opp in active_opps}
            matched = app_state.get("matched_events", [])
            kalshi_ids: list[str] = []
            kalshi_event_map: dict[str, SportEvent] = {}

            for event in matched:
                if event.team_a not in arb_teams:
                    continue
                km = event.markets.get(Platform.KALSHI)
                if km:
                    kalshi_ids.append(km.market_id)
                    kalshi_event_map[km.market_id] = event

            if not kalshi_ids:
                continue

            # Poll fresh prices
            fresh_prices = await kalshi.poll_active_markets(kalshi_ids)
            updated = 0
            for mid, price in fresh_prices.items():
                event = kalshi_event_map.get(mid)
                if event:
                    km = event.markets.get(Platform.KALSHI)
                    if km:
                        km.price = price
                        updated += 1

            if updated:
                logger.debug(f"Kalshi poller: refreshed {updated}/{len(kalshi_ids)} prices")

        except Exception:
            logger.exception("Kalshi price poller error")
            await asyncio.sleep(10)


def _kill_existing_on_port(port: int) -> None:
    """Kill any existing process on the given port (Linux only)."""
    import subprocess
    try:
        # Find and kill process using the port
        result = subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            logger.info(f"Killed existing process on port {port}")
            import time
            time.sleep(2)  # Wait for port to be released
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # fuser not available or timed out


async def run_app() -> None:
    """Start connectors, DB, web server, and scan loop."""
    # Kill any existing process on our port to avoid conflicts
    _kill_existing_on_port(settings.port)

    # Init DB
    await db.connect()
    logger.info(f"Database connected: {settings.db_path}")

    # Purge stale active arbs from previous run — they'll be re-detected if still valid
    purged = await db.deactivate_all_active()
    if purged:
        logger.info(f"Startup: deactivated {purged} stale arbs from previous run")

    # Init connectors
    poly = PolymarketConnector()
    kalshi = KalshiConnector()
    await poly.connect()
    await kalshi.connect()

    # Store connectors in app_state for dashboard access
    app_state["poly_connector"] = poly
    app_state["kalshi_connector"] = kalshi

    # Init executor settings manager
    from src.executor import ExecutorSettingsManager, ExecutorWSHandler, TradeLogger
    settings_manager = ExecutorSettingsManager(db)
    await settings_manager.load()
    app_state["executor_settings_manager"] = settings_manager

    # Init WebSocket handler for executor dashboard
    ws_handler = ExecutorWSHandler(
        settings_manager=settings_manager,
        db=db,
        poly_connector=poly,
        kalshi_connector=kalshi,
    )
    app_state["executor_ws_handler"] = ws_handler

    # Init trade logger
    trade_logger = TradeLogger(db=db, ws_handler=ws_handler)
    app_state["trade_logger"] = trade_logger

    # Init executor if enabled
    global _executor
    if settings.executor_enabled:
        try:
            from src.executor import (
                Executor, RiskManager, OrderPlacer,
                PositionManager, TelegramNotifier
            )
            # Use settings from SettingsManager
            exec_settings = settings_manager.get()
            risk_manager = RiskManager(
                min_bet=exec_settings.min_bet,
                max_bet=exec_settings.max_bet,
                min_roi=exec_settings.min_roi,
                max_roi=exec_settings.max_roi,
                max_daily_trades=exec_settings.max_daily_trades,
                max_daily_loss=exec_settings.max_daily_loss,
            )
            # Set executor enabled state from DB settings
            risk_manager.enabled = exec_settings.enabled
            order_placer = OrderPlacer(poly, kalshi)
            position_manager = PositionManager()  # Uses settings.db_path automatically
            await position_manager.connect()
            telegram = TelegramNotifier(
                settings.telegram_bot_token,
                settings.telegram_chat_id
            )
            _executor = Executor(
                risk_manager=risk_manager,
                order_placer=order_placer,
                position_manager=position_manager,
                telegram=telegram,
                poly_connector=poly,
                kalshi_connector=kalshi,
            )
            logger.info("Executor initialized and ENABLED")
        except Exception as e:
            logger.error(f"Failed to initialize executor: {e}")
            _executor = None
    else:
        logger.info("Executor is DISABLED (set EXECUTOR_ENABLED=true to enable)")

    # Setup web routes (deferred to avoid circular import)
    from src.web.app import setup_routes
    from src.web.routes import set_executor_ws_handler
    setup_routes()
    set_executor_ws_handler(ws_handler)

    # Start web server
    config = uvicorn.Config(
        "src.web.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    # Handle shutdown
    def shutdown_handler(sig, frame):
        logger.info("Shutdown signal received")
        app_state["running"] = False

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Run web server, scan loop, WS price listener, and Kalshi poller concurrently
    try:
        await asyncio.gather(
            server.serve(),
            scan_loop(poly, kalshi),
            ws_price_listener(poly),
            kalshi_price_poller(kalshi),
        )
    finally:
        await poly.disconnect()
        await kalshi.disconnect()
        await db.close()
        logger.info("Shutdown complete")


def main():
    asyncio.run(run_app())


if __name__ == "__main__":
    main()

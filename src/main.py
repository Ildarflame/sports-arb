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
from src.engine.arbitrage import calculate_arbitrage
from src.engine.matcher import match_events
from src.models import MarketPrice, Platform, SportEvent
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

# Concurrency limiter for price fetches
_price_semaphore = asyncio.Semaphore(10)

# Token → event mapping for O(1) WS price application
_token_to_event: dict[str, SportEvent] = {}


async def _fetch_with_semaphore(coro):
    """Run a coroutine with semaphore-limited concurrency."""
    async with _price_semaphore:
        return await coro


def _is_valid_price(price: MarketPrice | None) -> bool:
    """Check if a price looks real (not a 50/50 default placeholder)."""
    if not price:
        return False
    # Exactly 0.5/0.5 with no bid/ask and no volume = placeholder
    if (price.yes_price == 0.5
            and price.no_price == 0.5
            and price.yes_bid is None
            and price.yes_ask is None
            and (price.volume or 0) == 0):
        return False
    return True


def _is_stale_event(event: SportEvent) -> bool:
    """Check if a game event has a date in the past (>1 day old)."""
    pm = event.markets.get(Platform.POLYMARKET)
    km = event.markets.get(Platform.KALSHI)
    game_date = (pm.game_date if pm else None) or (km.game_date if km else None)
    if game_date and game_date < date.today() - timedelta(days=1):
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

    # Apply batch results to events
    for event, token_id in poly_normal_tokens:
        pm = event.markets.get(Platform.POLYMARKET)
        if pm and token_id in batch_prices:
            pm.price = batch_prices[token_id]

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

    logger.info(
        f"Book fetch: {poly_updated}/{len(poly_tasks)} Poly books, "
        f"{kalshi_updated}/{len(kalshi_tasks)} Kalshi prices refreshed"
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
    """Apply cached WS prices to events before price fetching. Returns count updated."""
    ws_cache = app_state["ws_price_cache"]
    if not ws_cache:
        return 0
    updated = 0
    for event in events:
        pm = event.markets.get(Platform.POLYMARKET)
        if not pm or pm.price:
            continue  # already has a price, skip
        token_ids = pm.raw_data.get("clob_token_ids", [])
        if token_ids and token_ids[0] in ws_cache:
            pm.price = ws_cache[token_ids[0]]
            updated += 1
    return updated


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

            # Match events across platforms
            matched = match_events(poly_markets, kalshi_markets)
            app_state["matched_events"] = matched

            # Update WS subscriptions with current matched token_ids
            _update_ws_subscriptions(matched)

            if matched:
                # Pass 0.5: Apply any cached WS prices before full fetch
                ws_applied = _apply_ws_cache(matched)
                if ws_applied:
                    logger.info(f"WS cache: applied {ws_applied} cached prices")

                # Pass 1: Fetch midpoint prices for all matched events
                await fetch_and_update_prices(poly, kalshi, matched)

                # Pass 1.5: Screen candidates by midpoint cost (with 2% buffer for bid/ask spread)
                arb_candidates: list[SportEvent] = []
                for event in matched:
                    # Skip stale events (game already played)
                    if _is_stale_event(event):
                        continue
                    pm = event.markets.get(Platform.POLYMARKET)
                    km = event.markets.get(Platform.KALSHI)
                    if pm and km and pm.price and km.price:
                        pp, kp = pm.price, km.price
                        # Skip placeholder 50/50 prices (no real data)
                        if not _is_valid_price(pp) or not _is_valid_price(kp):
                            continue
                        # Skip extreme prices (<=2c or >=98c) — illiquid long-shot futures
                        if kp.yes_price <= 0.02 or kp.yes_price >= 0.98:
                            continue
                        if pp.yes_price <= 0.02 or pp.yes_price >= 0.98:
                            continue
                        # Skip dead markets where both platforms have zero volume
                        if (pp.volume or 0) == 0 and (kp.volume or 0) == 0:
                            continue
                        if event.teams_swapped:
                            # Swapped: Kalshi YES = opposite team, so invert
                            cost1 = pp.yes_price + (1 - kp.yes_price)
                            cost2 = kp.no_price + pp.no_price
                        else:
                            cost1 = pp.yes_price + kp.no_price
                            cost2 = kp.yes_price + pp.no_price
                        if cost1 < 1.02 or cost2 < 1.02:
                            arb_candidates.append(event)

                # Pass 2: Fetch order books only for candidates (bid/ask precision)
                if arb_candidates:
                    logger.info(
                        f"Arb candidates: {len(arb_candidates)}/{len(matched)} "
                        f"passed midpoint screen, fetching order books"
                    )
                    await fetch_books_for_candidates(poly, kalshi, arb_candidates)

                # Track which arbs are found this scan (for deactivation)
                current_arb_keys: set[tuple[str, str, str]] = set()

                # Pass 3: Calculate arbitrage with executable prices
                for event in matched:
                    opp = calculate_arbitrage(event)
                    if opp and opp.roi_after_fees >= settings.min_arb_percent:
                        # Skip suspicious arbs entirely — don't save to DB
                        if opp.roi_after_fees > settings.max_arb_percent:
                            logger.warning(
                                f"SUSPICIOUS ARB (skipped, ROI>{settings.max_arb_percent}%): "
                                f"{opp.event_title} ROI={opp.roi_after_fees}%"
                            )
                            continue

                        arb_key = (
                            opp.team_a,
                            opp.platform_buy_yes.value,
                            opp.platform_buy_no.value,
                        )
                        current_arb_keys.add(arb_key)

                        opp_id = await db.save_opportunity(opp)
                        logger.info(
                            f"ARBITRAGE SAVED: {opp.event_title} "
                            f"ROI={opp.roi_after_fees}% id={opp_id}"
                        )
                        broadcast_event("new_arb", {
                            "event": opp.event_title,
                            "roi": opp.roi_after_fees,
                            "cost": opp.total_cost,
                        })

                # Deactivate stale opportunities not found this scan
                active_keys = await db.get_active_opp_keys()
                for key, opp_id in active_keys.items():
                    if key not in current_arb_keys:
                        n = await db.deactivate_by_key(*key)
                        logger.info(f"Deactivated stale arbs: {n} entries for key={key}")

                broadcast_event("price_update", {
                    "matched_count": len(matched),
                })

            # Batch commit all DB writes from this scan cycle
            await db.commit()

            # Periodic cleanup: delete old inactive opportunities every 100 scans
            _scan_count += 1
            if _scan_count % 100 == 0:
                deleted = await db.cleanup_old(days=7)
                if deleted:
                    logger.info(f"DB cleanup: removed {deleted} old inactive opportunities")
                await db.commit()

            scan_duration = time.monotonic() - scan_start
            app_state["last_scan_duration"] = round(scan_duration, 1)
            logger.info(f"Scan completed in {scan_duration:.1f}s")

        except Exception:
            logger.exception("Error in scan loop")

        await asyncio.sleep(settings.poll_interval)


async def run_app() -> None:
    """Start connectors, DB, web server, and scan loop."""
    # Init DB
    await db.connect()
    logger.info(f"Database connected: {settings.db_path}")

    # Init connectors
    poly = PolymarketConnector()
    kalshi = KalshiConnector()
    await poly.connect()
    await kalshi.connect()

    # Setup web routes (deferred to avoid circular import)
    from src.web.app import setup_routes
    setup_routes()

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

    # Run web server, scan loop, and WS price listener concurrently
    try:
        await asyncio.gather(
            server.serve(),
            scan_loop(poly, kalshi),
            ws_price_listener(poly),
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

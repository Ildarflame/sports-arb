from __future__ import annotations

import asyncio
import logging
import signal
import time

import uvicorn

from src.config import settings
from src.connectors.kalshi import KalshiConnector
from src.connectors.polymarket import PolymarketConnector
from src.db import db
from src.engine.arbitrage import calculate_arbitrage
from src.engine.matcher import match_events
from src.models import Platform, SportEvent
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
_price_semaphore = asyncio.Semaphore(20)


async def _fetch_with_semaphore(coro):
    """Run a coroutine with semaphore-limited concurrency."""
    async with _price_semaphore:
        return await coro


async def fetch_and_update_prices(
    poly: PolymarketConnector,
    kalshi: KalshiConnector,
    events: list[SportEvent],
) -> None:
    """Fetch latest prices for all matched events using batch/parallel fetching."""
    # Collect all price fetch tasks across all events
    poly_normal_tokens: list[tuple[SportEvent, str]] = []  # (event, token_id)
    poly_neg_risk_ids: list[tuple[SportEvent, str]] = []   # (event, market_id)
    kalshi_ids: list[tuple[SportEvent, str]] = []           # (event, market_id)

    for event in events:
        pm = event.markets.get(Platform.POLYMARKET)
        km = event.markets.get(Platform.KALSHI)

        if pm:
            token_ids = pm.raw_data.get("clob_token_ids", [])
            price_id = token_ids[0] if token_ids else pm.market_id
            is_neg_risk = pm.raw_data.get("neg_risk", False)
            if is_neg_risk:
                poly_neg_risk_ids.append((event, price_id))
            else:
                poly_normal_tokens.append((event, price_id))
        if km:
            kalshi_ids.append((event, km.market_id))

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
        task = asyncio.ensure_future(
            _fetch_with_semaphore(poly.fetch_price(market_id, neg_risk=True))
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

    for event, market_id in kalshi_ids:
        task = asyncio.ensure_future(
            _fetch_with_semaphore(kalshi.fetch_price(market_id))
        )
        individual_tasks.append(("kalshi", event, market_id, task))

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
            elif label == "kalshi":
                km = event.markets.get(Platform.KALSHI)
                if km:
                    km.price = result

        logger.info(f"Individual price fetches: {len(individual_tasks)} (negRisk + Kalshi)")


def broadcast_event(event_type: str, data: dict) -> None:
    from src.web.routes import broadcast_event as _broadcast
    _broadcast(event_type, data)


async def scan_loop(poly: PolymarketConnector, kalshi: KalshiConnector) -> None:
    """Main scanning loop: fetch events, match, check arbitrage."""
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

            if matched:
                # Fetch prices for matched events
                await fetch_and_update_prices(poly, kalshi, matched)

                # Track which arbs are found this scan (for deactivation)
                current_arb_keys: set[tuple[str, str, str]] = set()

                # Check for arbitrage
                for event in matched:
                    opp = calculate_arbitrage(event)
                    if opp and opp.roi_after_fees >= settings.min_arb_percent:
                        # Skip if ROI suspiciously high
                        if opp.roi_after_fees > settings.max_arb_percent:
                            opp.details["suspicious"] = True
                            logger.warning(
                                f"SUSPICIOUS ARB (ROI>{settings.max_arb_percent}%): "
                                f"{opp.event_title} ROI={opp.roi_after_fees}%"
                            )

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
                        await db.deactivate_opportunity(opp_id)
                        logger.info(f"Deactivated stale arb: {opp_id} key={key}")

                broadcast_event("price_update", {
                    "matched_count": len(matched),
                })

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

    # Run web server and scan loop concurrently
    try:
        await asyncio.gather(
            server.serve(),
            scan_loop(poly, kalshi),
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

from __future__ import annotations

import asyncio
import logging
import signal
import sys

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


async def fetch_and_update_prices(
    poly: PolymarketConnector,
    kalshi: KalshiConnector,
    events: list[SportEvent],
) -> None:
    """Fetch latest prices for all matched events."""
    for event in events:
        pm = event.markets.get(Platform.POLYMARKET)
        km = event.markets.get(Platform.KALSHI)

        tasks = []
        if pm:
            # Use first CLOB token ID if available, else market_id
            token_ids = pm.raw_data.get("clob_token_ids", [])
            price_id = token_ids[0] if token_ids else pm.market_id
            is_neg_risk = pm.raw_data.get("neg_risk", False)
            tasks.append(("poly", poly.fetch_price(price_id, neg_risk=is_neg_risk)))
        if km:
            tasks.append(("kalshi", kalshi.fetch_price(km.market_id)))

        results = await asyncio.gather(
            *[t[1] for t in tasks], return_exceptions=True
        )

        for (label, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                logger.warning(f"Price fetch error ({label}): {result}")
                continue
            if result is None:
                continue
            if label == "poly" and pm:
                pm.price = result
            elif label == "kalshi" and km:
                km.price = result


def broadcast_event(event_type: str, data: dict) -> None:
    from src.web.routes import broadcast_event as _broadcast
    _broadcast(event_type, data)


async def scan_loop(poly: PolymarketConnector, kalshi: KalshiConnector) -> None:
    """Main scanning loop: fetch events, match, check arbitrage."""
    while app_state["running"]:
        try:
            logger.info("--- Scanning for events ---")

            # Fetch sports events from both platforms
            poly_markets, kalshi_markets = await asyncio.gather(
                poly.fetch_sports_events(),
                kalshi.fetch_sports_events(),
            )

            logger.info(
                f"Fetched {len(poly_markets)} Polymarket, {len(kalshi_markets)} Kalshi markets"
            )

            # Match events across platforms
            matched = match_events(poly_markets, kalshi_markets)
            app_state["matched_events"] = matched

            if matched:
                # Fetch prices for matched events
                await fetch_and_update_prices(poly, kalshi, matched)

                # Check for arbitrage
                for event in matched:
                    opp = calculate_arbitrage(event)
                    if opp and opp.roi_after_fees >= settings.min_arb_percent:
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

                broadcast_event("price_update", {
                    "matched_count": len(matched),
                })

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

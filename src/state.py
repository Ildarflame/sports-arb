"""Shared application state accessible by all modules."""

app_state: dict = {
    "matched_events": [],
    "running": True,
    # Market caches
    "kalshi_cache": [],
    "kalshi_cache_time": 0.0,
    "poly_cache": [],
    "poly_cache_time": 0.0,
    # Metrics
    "poly_count": 0,
    "kalshi_count": 0,
    "last_scan_duration": 0.0,
    # WebSocket price streaming
    "ws_price_cache": {},        # token_id -> MarketPrice
    "ws_subscribed_ids": set(),  # currently subscribed token_ids
    "ws_update_count": 0,        # total WS price updates received
    # Tag discovery
    "last_tag_discovery": 0.0,   # monotonic timestamp of last tag discovery run
    # Per-sport scan timing
    "scan_metrics_by_sport": {},  # sport -> duration in seconds
    # Executor components (set in main.py)
    "executor_settings_manager": None,
    "executor_ws_handler": None,
    "trade_logger": None,
    "poly_connector": None,
    "kalshi_connector": None,
}

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Sports arbitrage detector that continuously scans Polymarket and Kalshi prediction markets for price discrepancies on the same sporting events. Detects when buying YES on one platform + NO on the other yields guaranteed profit after fees.

## Commands

```bash
# Install dependencies
uv sync

# Run the application (starts scan loop + web server on :8000)
uv run python -m src.main

# Run all tests
uv run pytest tests/ -v

# Run a single test file
uv run pytest tests/test_matcher.py -v

# Run a specific test
uv run pytest tests/test_matcher.py::test_function_name -v

# Deploy to server
./deploy.sh
```

## Architecture

**Scan loop** (`src/main.py`): Runs every 10 seconds. Fetches markets from both platforms → matches events → screens candidates at midpoint prices → fetches order books for candidates → calculates arbitrage ROI → stores in SQLite.

**Connectors** (`src/connectors/`): Platform-specific API clients. Polymarket uses Gamma API for markets + CLOB WebSocket for real-time prices. Kalshi uses REST with RSA-PSS signature auth. Both return normalized `Market` objects.

**Matcher** (`src/engine/matcher.py`): The most complex module (795 lines). Pre-groups markets by (sport, market_type), then fuzzy-matches team names using `rapidfuzz.token_sort_ratio()`. Key behaviors:
- Sport-specific team name normalization (NBA/NFL aliases, soccer club suffix stripping, NCAA mascot removal, umlaut normalization)
- Two match types: **game** (daily match, ±1 day tolerance) and **futures** (season/tournament, matched by event group)
- `teams_swapped` flag set when platforms list teams in opposite order — but **blocked for 3-outcome sports** (soccer/rugby/cricket) where price inversion is invalid due to draws
- Match thresholds: 75% for 2-team, 93% for single-team futures

**Arbitrage calculator** (`src/engine/arbitrage.py`): Checks both trade directions (buy YES on Platform A + NO on Platform B, and vice versa). Uses executable bid/ask prices when available, falls back to midpoint. Applies platform fees (~2% Polymarket, ~1.5% Kalshi). Flags suspicious opportunities (ROI >100%, wide spreads, zero volume).

**Database** (`src/db.py`): SQLite via aiosqlite. Three tables: `events`, `market_prices`, `opportunities`. Deduplicates opportunities by (team_a, platform_buy_yes, platform_buy_no) key. Auto-deactivates stale opportunities each scan.

**Web server** (`src/web/`): FastAPI with Jinja2 templates. Dashboard at `/`, REST API at `/api/events` and `/api/opportunities`, SSE stream at `/api/stream` for live updates.

**Global state** (`src/state.py`): Single `app_state` dict holds market caches (Kalshi 10min TTL, Poly 5min), WebSocket price cache, matched events, and scan metrics.

## Key Design Decisions

- **Two-pass screening**: Pass 1 uses cheap midpoint prices to filter candidates (cost < 1.02), Pass 2 fetches expensive order book data only for candidates. This minimizes API calls.
- **WebSocket price streaming**: Polymarket CLOB WebSocket provides real-time price updates for matched markets, reducing stale price issues.
- **Concurrency**: Semaphore-limited to 10 concurrent price fetches. Scan loop and WS listener run concurrently via `asyncio.gather()`.
- **3-outcome sport blocking**: Soccer/rugby/cricket matches never get `teams_swapped=True` because YES Team A + NO Team B doesn't cover draws, creating fake arbitrage.

## Configuration

Copy `.env.example` to `.env`. Required: `KALSHI_EMAIL` and `KALSHI_PASSWORD` (or RSA key via `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH`). Polymarket APIs are public (no auth for read).

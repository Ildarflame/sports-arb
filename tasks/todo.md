# Extended Arbitrage Types - Implementation Status

## Final Review (2026-02-04)

All 5 modules from the original plan are **COMPLETE** ✅

---

## Module 1: Cross-Team Arbitrage ✅

**Status:** Implemented

**Implementation:**
- `src/engine/arbitrage.py:353-510`: Directions 3-4 for YES_A + YES_B arbitrage
- Badge: `CROSS-TEAM` (purple) in `alerts.html:61-62`
- Only for 2-outcome sports (not soccer/rugby/cricket)

**How it works:**
- Direction 3: Buy Poly YES (team_a) + Kalshi YES (team_b)
- Direction 4: Buy Poly NO (team_b) + Kalshi NO (team_a)
- If total cost < 1.0, one team must win → guaranteed profit

---

## Module 2: 3-Way Arbitrage ✅

**Status:** Implemented

**Implementation:**
- `src/engine/matcher.py`: 3-way grouping via `ThreeWayGroup` model
- `src/engine/arbitrage.py:663-795`: `calculate_3way_arbitrage()` function
- Badge: `3-WAY` (green) in `alerts.html:63-64`

**How it works:**
- Groups Win_A + Draw + Win_B markets for soccer/rugby/cricket
- Picks best price for each outcome across platforms
- If total cost < 1.0 → arbitrage

---

## Module 3: Live Mode ✅

**Status:** Implemented (enabled by default)

**Configuration (`src/config.py:26-30`):**
```python
allow_live_arbs: bool = True      # Allow arbs on in-progress games
live_min_confidence: str = "high" # Minimum confidence for live arbs
live_max_spread_pct: float = 10.0 # Maximum spread % for live arbs
live_max_roi: float = 50.0        # Maximum ROI (high = suspicious)
```

**Implementation:**
- `_is_market_expired()`: Returns `(is_expired, is_live)` tuple
- `_validate_live_arb()`: Strict validation for live arbs
  - Requires `executable=True` (real bid/ask data)
  - Requires `confidence >= live_min_confidence`
  - Requires `spread_pct <= live_max_spread_pct`
  - Requires `roi <= live_max_roi`
  - Rejects suspicious arbs
- Badge: `LIVE` (red, pulsing) in `alerts.html:66-68`
- Logging: `[LIVE]` tag in `main.py:604-605`

---

## Module 4: Spread/O-U QA ✅

**Status:** Infrastructure complete, no markets available

**Implementation:**
- Detection: `_detect_market_subtype()` in both connectors
- Matching: Exact line match required in `matcher.py:726-727`
- Badges: `SPREAD` (orange), `O/U` (teal) in `alerts.html:69-72`

**Findings:**
- Polymarket: 0 spread/OU markets (all `sportsMarketType: none`)
- Kalshi: 0 spread/OU markets
- System will auto-detect when platforms start offering these

---

## Module 5: Map Winner (Esports) ✅

**Status:** Implemented

**Implementation:**
- Detection: `_detect_map_number()` in both connectors
- Matching: `matcher.py:726-727` checks `map_number` equality
- Badge: `MAP N` (cyan) in `alerts.html:73-74`

**Pattern matching:**
- Matches: "Map 1", "map 2", "MAP3", "map-1"
- Stores: `market_subtype = "map_winner"`, `map_number = N`

---

## System Status

| Component | Status |
|-----------|--------|
| Cross-team arbs | ✅ Working |
| 3-way arbs | ✅ Working (380 groups, 51 cross-platform) |
| Live mode | ✅ Enabled by default |
| Spread/O-U | ✅ Ready (no markets yet) |
| Map winner | ✅ Ready |
| All tests | ✅ 29/29 passing |

---

## Key Files

| File | Purpose |
|------|---------|
| `src/config.py` | Live mode settings |
| `src/engine/arbitrage.py` | All arb calculations (2-way, 3-way, cross-team) |
| `src/engine/matcher.py` | Market grouping and matching |
| `src/connectors/polymarket.py` | Market subtype detection |
| `src/connectors/kalshi.py` | Market subtype detection |
| `src/web/templates/partials/alerts.html` | UI badges |

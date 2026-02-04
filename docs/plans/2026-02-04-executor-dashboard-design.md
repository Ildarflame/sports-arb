# Executor Dashboard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add comprehensive executor control and monitoring to the dashboard with improved arbitrage UX.

**Architecture:** WebSocket-based real-time updates, ExecutorSettingsManager with SQLite persistence + memory cache, modular `/executor` page with settings/positions/logs panels.

**Tech Stack:** FastAPI, WebSocket, Jinja2, SQLite, asyncio, SSE

---

## Section 1: Architecture Overview

### Components

1. **ExecutorSettingsManager** (`src/executor/settings_manager.py`)
   - Memory cache for fast reads
   - SQLite table for persistence across restarts
   - Atomic update operations
   - Settings: enabled, min_bet, max_bet, min_roi, max_roi, max_daily_trades, max_daily_loss

2. **WebSocket Endpoint** (`/ws/executor`)
   - Real-time bidirectional communication
   - Push: balance updates, trade events, position changes, settings changes
   - Receive: settings updates, enable/disable commands, manual position close

3. **Database Tables**
   - `executor_settings` - single row with all settings
   - `executor_trades` - trade history log
   - `executor_positions` - currently open positions

4. **Frontend**
   - `/executor` page with WebSocket connection
   - Real-time updates without page refresh
   - Settings form with instant feedback

---

## Section 2: /executor Page UI Layout

### Header Section
- Status badge: "ACTIVE" (green) / "PAUSED" (red)
- Large toggle button to enable/disable
- Last update timestamp

### Three-Column Layout

**Column 1: Settings Panel**
```
┌─────────────────────────┐
│ Settings                │
├─────────────────────────┤
│ Min Bet: [$___5.00___]  │
│ Max Bet: [$__10.00___]  │
│ Min ROI: [___1.0___%]   │
│ Max ROI: [__50.0___%]   │
│ Max Daily Trades: [_50_]│
│ Max Daily Loss: [$5.00_]│
├─────────────────────────┤
│ [Save Settings]         │
└─────────────────────────┘
```

**Column 2: Balances & Stats**
```
┌─────────────────────────┐
│ Balances                │
├─────────────────────────┤
│ Polymarket:   $XX.XX    │
│ Kalshi:       $XX.XX    │
│ Total:        $XX.XX    │
├─────────────────────────┤
│ Today's Stats           │
├─────────────────────────┤
│ Trades: X / 50          │
│ P&L: +$X.XX             │
│ Win Rate: XX%           │
└─────────────────────────┘
```

**Column 3: Open Positions**
```
┌─────────────────────────┐
│ Open Positions (X)      │
├─────────────────────────┤
│ LAL vs BOS              │
│   Poly YES @ 0.51       │
│   Kalshi NO @ 0.48      │
│   [Close Position]      │
├─────────────────────────┤
│ ...                     │
└─────────────────────────┘
```

### Bottom Section: Trade Log
```
┌─────────────────────────────────────────────────────────────────┐
│ Recent Trades                                        [Clear]    │
├─────────────────────────────────────────────────────────────────┤
│ 14:32:15 | SUCCESS | LAL vs BOS | $5.00 | +$0.12 | 2.4% ROI    │
│ 14:28:03 | ROLLED_BACK | MIA vs NYK | $5.00 | -$0.80 | Poly fail│
│ ...                                                             │
└─────────────────────────────────────────────────────────────────┘
```

---

## Section 3: WebSocket Protocol

### Connection
```
ws://host/ws/executor
```

### Server → Client Messages

```json
// Initial state on connect
{
  "type": "init",
  "data": {
    "enabled": true,
    "settings": {...},
    "balances": {"poly": 50.0, "kalshi": 45.0},
    "stats": {"trades": 3, "pnl": 1.25, "wins": 2},
    "positions": [...]
  }
}

// Real-time updates
{"type": "balance_update", "data": {"poly": 49.5, "kalshi": 44.2}}
{"type": "trade_event", "data": {"event": "LAL vs BOS", "status": "SUCCESS", ...}}
{"type": "position_opened", "data": {...}}
{"type": "position_closed", "data": {...}}
{"type": "settings_changed", "data": {...}}
{"type": "status_changed", "data": {"enabled": false}}
```

### Client → Server Messages

```json
{"action": "toggle_enabled", "value": true}
{"action": "update_settings", "settings": {"min_bet": 5.0, "max_bet": 15.0}}
{"action": "close_position", "position_id": "abc123"}
```

### Error Handling
```json
{"type": "error", "message": "Invalid settings value", "field": "min_bet"}
```

---

## Section 4: Arbitrage Card UX Improvements

### Current Issues
- Cards look the same regardless of quality
- No filtering capability
- Information overload

### Improvements

**1. Visual Confidence Indicators**
```
HIGH:   Green left border (4px) + subtle green background
MEDIUM: Yellow left border + subtle yellow background
LOW:    Red left border + subtle red background
```

**2. Quick Filters (top of list)**
```
[All] [HIGH only] [MEDIUM+] | [Hide expired] | Sort: [ROI ▼]
```

**3. Streamlined Card Layout**
```
┌──────────────────────────────────────────┐
│ 🏀 Lakers vs Celtics            HIGH ●   │
│ ─────────────────────────────────────────│
│ BUY YES Poly @ 0.51  →  BUY NO Kalshi @ 0.48
│ ─────────────────────────────────────────│
│ ROI: 2.5%  |  Cost: $0.99  |  Vol: 1.2K  │
│ ─────────────────────────────────────────│
│ [Copy Trade] [Details ▼]                 │
└──────────────────────────────────────────┘
```

**4. Expandable Details**
- Token IDs, URLs, timestamps hidden by default
- "Details ▼" expands to show full info
- Collapsed view shows only actionable info

**5. Color-Coded ROI**
- ROI < 1%: gray
- ROI 1-3%: green
- ROI 3-5%: bright green
- ROI > 5%: gold (suspicious)

---

## Section 5: Database Schema Changes

### New Tables

```sql
-- Executor settings (single row)
CREATE TABLE executor_settings (
    id INTEGER PRIMARY KEY DEFAULT 1,
    enabled BOOLEAN DEFAULT FALSE,
    min_bet REAL DEFAULT 5.0,
    max_bet REAL DEFAULT 10.0,
    min_roi REAL DEFAULT 1.0,
    max_roi REAL DEFAULT 50.0,
    max_daily_trades INTEGER DEFAULT 50,
    max_daily_loss REAL DEFAULT 5.0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (id = 1)  -- Ensure single row
);

-- Trade history
CREATE TABLE executor_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    event_title TEXT NOT NULL,
    status TEXT NOT NULL,  -- SUCCESS, FAILED, ROLLED_BACK
    bet_size REAL NOT NULL,
    pnl REAL DEFAULT 0,
    roi REAL,
    poly_order_id TEXT,
    kalshi_order_id TEXT,
    details JSON
);

-- Open positions
CREATE TABLE executor_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT UNIQUE NOT NULL,
    event_title TEXT NOT NULL,
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    poly_side TEXT,
    poly_price REAL,
    poly_contracts INTEGER,
    kalshi_side TEXT,
    kalshi_price REAL,
    kalshi_contracts INTEGER,
    status TEXT DEFAULT 'open'  -- open, closed, expired
);

-- Indexes
CREATE INDEX idx_trades_timestamp ON executor_trades(timestamp);
CREATE INDEX idx_positions_status ON executor_positions(status);
```

### Migration Strategy
1. Add tables without disrupting existing schema
2. Initialize `executor_settings` with current config.py defaults
3. Backfill `executor_trades` from any existing logs (if available)

---

## Section 6: File Structure & Implementation Order

### New Files
```
src/executor/
├── settings_manager.py     # ExecutorSettingsManager class
├── ws_handler.py           # WebSocket connection handler
└── trade_logger.py         # Trade history persistence

src/web/
├── routes/
│   └── executor.py         # /executor page + /ws/executor endpoint
└── templates/
    └── executor.html       # /executor page template

src/db.py                   # Add new table migrations
```

### Modified Files
```
src/executor/auto_executor.py  # Use SettingsManager instead of config
src/executor/risk_manager.py   # Dynamic settings from manager
src/web/routes/api.py          # Add executor status to /api/status
src/web/templates/index.html   # Add card UX improvements
src/web/static/css/style.css   # Confidence colors, card styling
src/main.py                    # Initialize SettingsManager, WS handler
```

### Implementation Order
1. **Database**: Add `executor_settings`, `executor_trades`, `executor_positions` tables
2. **ExecutorSettingsManager**: Memory cache + SQLite persistence
3. **WebSocket handler**: `/ws/executor` endpoint with protocol
4. **Trade logger**: Persist trades, manage positions
5. **/executor page**: HTML template + JavaScript WebSocket client
6. **Dashboard UX**: Card improvements, filters, confidence colors
7. **Integration**: Wire everything together in main.py

---

## Testing Strategy

### Unit Tests
- `test_settings_manager.py` - CRUD operations, persistence
- `test_ws_handler.py` - Message parsing, state updates
- `test_trade_logger.py` - Trade recording, position tracking

### Integration Tests
- WebSocket connection lifecycle
- Settings persistence across restart
- Real-time update propagation

### Manual Testing
- Enable/disable via UI while trades happening
- Settings change mid-execution
- Position close during active trade

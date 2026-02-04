# Auto Executor Design

**Date:** 2026-02-04
**Status:** Approved

## Overview

Fully automatic arbitrage execution system that detects opportunities and executes trades on both Polymarket and Kalshi without manual intervention.

## Requirements

- **Automation level:** Fully automatic
- **Platforms:** Polymarket + Kalshi (API keys ready)
- **Bet size:** $1-2 per trade (testing phase)
- **Arb types:** All (YES+NO, cross-team, 3-way, live)
- **Notifications:** Telegram
- **Minimum ROI:** 1-2%

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     CURRENT SYSTEM                          │
│  Connectors → Matcher → Arbitrage Calculator → Web UI       │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼ ArbitrageOpportunity
┌─────────────────────────────────────────────────────────────┐
│                   NEW MODULE: EXECUTOR                      │
│                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │Risk Manager  │───▶│Order Placer  │───▶│  Telegram    │  │
│  │ - checks     │    │ - Poly API   │    │  Notifier    │  │
│  │ - limits     │    │ - Kalshi API │    │              │  │
│  └──────────────┘    └──────────────┘    └──────────────┘  │
│         │                   │                              │
│         ▼                   ▼                              │
│  ┌──────────────┐    ┌──────────────┐                      │
│  │Balance       │    │Position      │                      │
│  │Tracker       │    │Manager       │                      │
│  └──────────────┘    └──────────────┘                      │
└─────────────────────────────────────────────────────────────┘
```

**Flow:**
1. Calculator finds arbitrage with ROI ≥ 1%
2. Risk Manager checks: balance sufficient? limits ok? no duplicate position?
3. Order Placer sends BOTH legs simultaneously (parallel)
4. Telegram sends result: executed / partial / error
5. Position Manager tracks open positions until settlement

---

## Component: Risk Manager

```python
class RiskLimits:
    # Bet size
    min_bet: float = 1.0          # Minimum $1
    max_bet: float = 2.0          # Maximum $2

    # ROI filters
    min_roi: float = 1.0          # Minimum 1%
    max_roi: float = 50.0         # Maximum 50% (higher = suspicious)

    # Daily limits
    max_daily_trades: int = 50    # Max trades per day
    max_daily_loss: float = 5.0   # Stop at $5 loss

    # Balance
    min_platform_balance: float = 1.0  # Don't trade if < $1

    # Kill switch
    enabled: bool = True          # Global switch
```

**Pre-trade checks:**
1. Kill switch active?
2. Balance sufficient on both platforms?
3. ROI in range (1-50%)?
4. Daily limits not exceeded?
5. No duplicate position on same match?
6. For live arbs: confidence=high, executable=True?

---

## Component: Order Placer

```python
async def execute_arbitrage(opp: ArbitrageOpportunity, bet_size: float):
    # 1. Calculate leg sizes
    poly_amount, kalshi_amount = calculate_leg_sizes(...)

    # 2. Execute BOTH orders simultaneously
    poly_task = place_polymarket_order(...)
    kalshi_task = place_kalshi_order(...)

    # 3. Wait for both results
    poly_result, kalshi_result = await asyncio.gather(
        poly_task, kalshi_task,
        return_exceptions=True
    )

    # 4. Process results
    return ExecutionResult(poly=poly_result, kalshi=kalshi_result)
```

**Possible outcomes:**

| Polymarket | Kalshi | Status | Action |
|------------|--------|--------|--------|
| ✅ Filled | ✅ Filled | SUCCESS | Arbitrage executed |
| ✅ Filled | ❌ Failed | PARTIAL | Alert, attempt hedge |
| ❌ Failed | ✅ Filled | PARTIAL | Alert, attempt hedge |
| ❌ Failed | ❌ Failed | FAILED | Nothing happened, OK |

---

## Component: Telegram Notifier

**Message types:**

```
✅ EXECUTED — Successful arbitrage
━━━━━━━━━━━━━━━━━━━━━━━━━
🏀 Lakers vs Celtics
📈 ROI: 2.3% ($0.04 profit)

Poly: BUY YES $1.02 @ 0.51
Kalshi: BUY NO $0.98 @ 0.49

💰 Balances: Poly $8.47 | Kalshi $9.12
```

```
⚠️ PARTIAL FILL — Attention required!
━━━━━━━━━━━━━━━━━━━━━━━━━
⚽ Arsenal vs Chelsea
Poly: ✅ Filled
Kalshi: ❌ Failed (insufficient liquidity)

🔴 Open position! Check manually.
```

```
🛑 KILL SWITCH ACTIVATED
━━━━━━━━━━━━━━━━━━━━━━━━━
Reason: Daily loss limit ($5) reached
Trades today: 23
P&L: -$5.12

Send /start to resume
```

**Bot commands:**

| Command | Action |
|---------|--------|
| `/status` | Current balances, open positions |
| `/stop` | Disable auto-trading |
| `/start` | Enable auto-trading |
| `/trades` | Last 10 trades |
| `/pnl` | P&L today/week/all-time |

---

## Component: Position Manager

```python
@dataclass
class OpenPosition:
    id: str
    event_title: str
    team_a: str
    team_b: str

    # Polymarket leg
    poly_side: str          # "YES" or "NO"
    poly_amount: float
    poly_contracts: float
    poly_avg_price: float

    # Kalshi leg
    kalshi_side: str
    kalshi_amount: float
    kalshi_contracts: int
    kalshi_avg_price: float

    # Meta
    arb_type: str           # "yes_no", "cross_team", "3way"
    expected_roi: float
    opened_at: datetime
    status: str             # "open", "settled", "partial"

    # Result (after settlement)
    settled_at: datetime | None
    actual_pnl: float | None
```

**Storage:** SQLite table `positions`

**Auto-settlement check:**
- Every 5 minutes check open positions
- If match finished → query result from platforms
- Calculate actual P&L and update stats
- Telegram: "🏁 Position settled: Lakers vs Celtics, P&L: +$0.04"

---

## File Structure

```
src/
├── executor/
│   ├── __init__.py
│   ├── risk_manager.py      # Checks and limits
│   ├── order_placer.py      # Order execution
│   ├── position_manager.py  # Position tracking
│   ├── telegram_bot.py      # Notifications and commands
│   └── balance_tracker.py   # Balance sync
│
├── connectors/
│   ├── polymarket.py        # + place_order(), get_balance()
│   └── kalshi.py            # + place_order(), get_balance()
│
└── config.py                # + executor settings
```

---

## Configuration (.env)

```bash
# Existing
KALSHI_API_KEY_ID=...
KALSHI_PRIVATE_KEY_PATH=...

# New: Polymarket trading
POLY_PRIVATE_KEY=...
POLY_API_KEY=...
POLY_API_SECRET=...

# New: Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# New: Executor settings
EXECUTOR_ENABLED=true
EXECUTOR_MIN_BET=1.0
EXECUTOR_MAX_BET=2.0
EXECUTOR_MIN_ROI=1.0
EXECUTOR_MAX_DAILY_TRADES=50
EXECUTOR_MAX_DAILY_LOSS=5.0
```

---

## Launch

```bash
# As now (detector + web)
uv run python -m src.main

# Executor runs automatically if EXECUTOR_ENABLED=true
```

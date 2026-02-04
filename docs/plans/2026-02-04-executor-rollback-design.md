# Executor Parallel Execution with Auto-Rollback

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix bet sizing, add parallel execution with automatic rollback on partial fills.

**Architecture:** Execute both platforms in parallel, rollback successful leg if other fails.

**Tech Stack:** Python asyncio, existing connectors

---

## Problems Being Solved

1. **Bet sizing bug**: Passing shares count instead of dollar amount to Polymarket API
2. **Partial fill risk**: When one platform succeeds and other fails, left with unhedged position
3. **Incorrect logging**: Showing planned amounts instead of actual filled amounts

---

## Design

### 1. Bet Sizing Fix

**Current (broken):**
```python
contracts = size / price  # $0.85 / 0.41 = 2.07
poly.place_order(size=contracts)  # API interprets as $2.07
```

**Fixed:**
```python
poly.place_order(size=poly_dollar_amount)  # Pass $0.85 directly
```

Polymarket `MarketOrderArgs.amount` expects dollars, not shares.

### 2. Parallel Execution with Rollback

```
┌─────────────────────────────────────────────────────┐
│  1. Execute in parallel: Poly + Kalshi              │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│  2. Wait for both results (asyncio.gather)          │
└─────────────────────────────────────────────────────┘
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
   Both ✅          One ✅           Both ❌
   ───────          One ❌           ──────
   Success!         ROLLBACK         Nothing
                    ────────         to do
                    │
                    ▼
              ┌───────────┐
              │ Sell the  │
              │ successful│
              │ (FOK sell)│
              └───────────┘
```

**Rollback logic:**
- Poly ✅, Kalshi ❌ → `poly.place_order(side="SELL", size=filled_amount)`
- Kalshi ✅, Poly ❌ → `kalshi.place_order(action="sell", count=filled_contracts)`

### 3. Updated LegResult Model

```python
@dataclass
class LegResult:
    platform: str
    success: bool
    order_id: str | None
    filled_shares: float      # actual shares/contracts filled
    filled_price: float       # actual avg price
    filled_cost: float        # shares × price (dollars spent)
    error: str | None = None
```

### 4. Telegram Message Format

**Success:**
```
✅ EXECUTED — Arbitrage locked in!
━━━━━━━━━━━━━━━━━━━━━━━━━
🏀 Lakers vs Celtics

📊 Positions:
Poly: 2.07 shares × $0.41 = $0.85
Kalshi: 3 contracts × $0.58 = $1.74
Total invested: $2.59

💰 Guaranteed payout: $3.00
📈 Profit: $0.41 (15.8% ROI)

💳 Balances: Poly $9.15 | Kalshi $8.26
```

**Rolled back:**
```
⚠️ PARTIAL FILL — Rolled back
━━━━━━━━━━━━━━━━━━━━━━━━━
🏀 Lakers vs Celtics

Poly: ✅ 2.07 shares × $0.41 = $0.85
Kalshi: ❌ FOK rejected (no liquidity)

🔄 Rollback: Sold Poly 2.07 shares @ $0.39
   Loss: $0.04 (spread)

💳 Balances: Poly $10.22 | Kalshi $10.00
```

---

## Files to Modify

| File | Changes |
|------|---------|
| `src/executor/order_placer.py` | Main logic: sizing fix, parallel + rollback |
| `src/executor/models.py` | Update `LegResult` with new fields |
| `src/executor/telegram_notifier.py` | New message format with payout |

---

## Implementation Tasks

### Task 1: Update LegResult model
- Add `filled_shares`, `filled_cost` fields
- Keep backward compatibility

### Task 2: Fix Polymarket bet sizing
- Remove `contracts = size / price` calculation
- Pass dollar amount directly to `place_order`
- Parse actual filled amount from API response

### Task 3: Fix Kalshi bet sizing
- Verify contract calculation is correct
- Parse actual filled amount from API response

### Task 4: Add rollback methods
- `_rollback_poly(leg_result)` - sell at market
- `_rollback_kalshi(leg_result)` - sell at market
- Both use FOK for immediate execution

### Task 5: Update execute() flow
- Keep parallel execution
- Add result checking logic
- Call rollback if partial fill
- Return appropriate status

### Task 6: Update Telegram messages
- Add payout calculation
- Show actual filled amounts
- Format rollback messages

### Task 7: Test and deploy
- Run tests
- Deploy to server
- Monitor first few executions

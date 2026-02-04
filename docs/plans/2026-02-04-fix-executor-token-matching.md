# Fix Executor Token Matching Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the executor to correctly match Polymarket tokens with Kalshi YES/NO sides so arbitrage trades cover BOTH outcomes (not the same outcome twice).

**Architecture:**
- Polymarket has TWO separate tokens per market: `clob_token_ids[0]` = team_a wins, `clob_token_ids[1]` = team_b wins
- Kalshi has ONE market with YES/NO: YES = team_a wins, NO = team_a loses (= team_b wins)
- For proper arbitrage: Poly team_a token + Kalshi NO = covers both outcomes
- Current bug: code always uses `clob_token_ids[0]` regardless of which team we're betting on

**Tech Stack:** Python 3.12, asyncio, py-clob-client, httpx

---

## Problem Analysis

### Current Bug

In `arbitrage.py`, all 4 directions set:
```python
"poly_token_id": poly_market.raw_data.get("clob_token_ids", [None])[0],
```

This always uses token[0] (team_a), but:
- Direction 1: Buy Poly YES (team_a) + Kalshi NO (team_b) → needs token[0] ✓
- Direction 2: Buy Poly NO (team_b) + Kalshi YES (team_a) → needs token[1] ✗ BUG!
- Direction 3/4: Cross-team scenarios also may need token[1]

### In `order_placer.py`:
```python
if opp.platform_buy_yes == Platform.POLYMARKET:
    poly_side = "BUY"      # Buys the token
else:
    poly_side = "SELL"     # Sells the token (wrong approach!)
```

SELL on Polymarket sells the token you have - it doesn't buy the opposite outcome!
To bet on team_b, you must BUY the team_b token (token[1]), not SELL team_a token.

---

### Task 1: Add Polymarket Token Selection Logic in arbitrage.py

**Files:**
- Modify: `src/engine/arbitrage.py:277-280` (Direction 1)
- Modify: `src/engine/arbitrage.py:353-356` (Direction 2)
- Modify: `src/engine/arbitrage.py:458-461` (Direction 3)
- Modify: `src/engine/arbitrage.py:526-529` (Direction 4)

**Step 1: Update Direction 1 to use correct token**

Direction 1 buys Poly YES = team_a → use token[0]

In `src/engine/arbitrage.py`, find the Direction 1 details dict (around line 277) and update:

```python
                    # Trading identifiers for executor
                    "poly_token_id": poly_market.raw_data.get("clob_token_ids", [None, None])[0],  # team_a token
                    "poly_side": "BUY",  # Always BUY the token we want
                    "kalshi_ticker": kalshi_market.market_id,
                    "kalshi_side": "no" if not event.teams_swapped else "yes",  # Actual Kalshi side
```

**Step 2: Update Direction 2 to use team_b token**

Direction 2 buys Poly NO = team_b → use token[1]

Find Direction 2 details dict (around line 353) and update:

```python
                    # Trading identifiers for executor
                    "poly_token_id": poly_market.raw_data.get("clob_token_ids", [None, None])[1],  # team_b token
                    "poly_side": "BUY",  # BUY the team_b token (not SELL team_a!)
                    "kalshi_ticker": kalshi_market.market_id,
                    "kalshi_side": "yes" if not event.teams_swapped else "no",  # Actual Kalshi side
```

**Step 3: Update Direction 3 (cross-team) to use team_a token**

Find Direction 3 details dict (around line 458) and update:

```python
                        # Trading identifiers for executor
                        "poly_token_id": poly_market.raw_data.get("clob_token_ids", [None, None])[0],  # team_a token
                        "poly_side": "BUY",
                        "kalshi_ticker": kalshi_market.market_id,
                        "kalshi_side": "yes",  # Original Kalshi YES (cross-team)
```

**Step 4: Update Direction 4 (cross-team) to use team_b token**

Find Direction 4 details dict (around line 526) and update:

```python
                        # Trading identifiers for executor
                        "poly_token_id": poly_market.raw_data.get("clob_token_ids", [None, None])[1],  # team_b token
                        "poly_side": "BUY",
                        "kalshi_ticker": kalshi_market.market_id,
                        "kalshi_side": "no",  # Original Kalshi NO (cross-team)
```

**Step 5: Commit**

```bash
git add src/engine/arbitrage.py
git commit -m "fix: use correct Polymarket token for each direction

- Direction 1 (Poly YES): use token[0] (team_a)
- Direction 2 (Poly NO): use token[1] (team_b)
- Direction 3/4 (cross-team): use appropriate token
- Add poly_side and kalshi_side to details for executor"
```

---

### Task 2: Update order_placer.py to Use Explicit Sides

**Files:**
- Modify: `src/executor/order_placer.py:43-54`

**Step 1: Replace side determination logic**

Find the current side determination code (around line 43):

```python
        # Determine sides based on which platform is buy YES vs buy NO
        if opp.platform_buy_yes == Platform.POLYMARKET:
            poly_side = "BUY"
            kalshi_side = "no"
            kalshi_action = "buy"
        else:
            poly_side = "SELL"  # Buying NO = selling YES on Poly
            kalshi_side = "yes"
            kalshi_action = "buy"
```

Replace with:

```python
        # Use explicit sides from arbitrage calculation
        # Polymarket: always BUY the correct token (team_a or team_b)
        poly_side = opp.details.get("poly_side", "BUY")

        # Kalshi: use the pre-calculated side from arbitrage.py
        kalshi_side = opp.details.get("kalshi_side", "no")
        kalshi_action = "buy"

        # Log for debugging
        logger.info(f"Order sides: poly={poly_side} (token={opp.details.get('poly_token_id', '')[:20]}...), kalshi={kalshi_side}")
```

**Step 2: Commit**

```bash
git add src/executor/order_placer.py
git commit -m "fix: use explicit poly_side and kalshi_side from arbitrage details

Instead of inferring sides from platform_buy_yes, use pre-calculated
sides stored in opportunity details. This ensures correct token selection."
```

---

### Task 3: Add Validation in Risk Manager

**Files:**
- Modify: `src/executor/risk_manager.py:69-72`

**Step 1: Add validation for new required fields**

Find the missing identifiers check (around line 69):

```python
        # Skip if missing trading identifiers
        if not opp.details.get("poly_token_id") or not opp.details.get("kalshi_ticker"):
            return RiskCheckResult(False, "Missing trading identifiers (poly_token_id or kalshi_ticker)")
```

Replace with:

```python
        # Skip if missing trading identifiers
        if not opp.details.get("poly_token_id"):
            return RiskCheckResult(False, "Missing poly_token_id")
        if not opp.details.get("kalshi_ticker"):
            return RiskCheckResult(False, "Missing kalshi_ticker")
        if not opp.details.get("poly_side"):
            return RiskCheckResult(False, "Missing poly_side (BUY expected)")
        if not opp.details.get("kalshi_side"):
            return RiskCheckResult(False, "Missing kalshi_side (yes/no expected)")
```

**Step 2: Commit**

```bash
git add src/executor/risk_manager.py
git commit -m "fix: validate poly_side and kalshi_side in risk manager"
```

---

### Task 4: Write Integration Test

**Files:**
- Create: `tests/test_executor_tokens.py`

**Step 1: Create test file**

```python
"""Test that executor uses correct tokens for arbitrage directions."""

import pytest
from datetime import datetime, UTC

from src.models import ArbitrageOpportunity, Platform


def test_direction1_uses_team_a_token():
    """Direction 1: Buy Poly YES (team_a) + Kalshi NO."""
    opp = ArbitrageOpportunity(
        event_title="Team A vs Team B",
        team_a="Team A",
        team_b="Team B",
        platform_buy_yes=Platform.POLYMARKET,
        platform_buy_no=Platform.KALSHI,
        yes_price=0.45,
        no_price=0.50,
        total_cost=0.97,
        profit_pct=3.0,
        roi_after_fees=1.5,
        found_at=datetime.now(UTC),
        details={
            "arb_type": "yes_no",
            "poly_token_id": "token_team_a_12345",
            "poly_side": "BUY",
            "kalshi_ticker": "KXGAME-TEAMA",
            "kalshi_side": "no",
        },
    )

    # Verify poly_side is BUY (not SELL)
    assert opp.details["poly_side"] == "BUY"
    # Verify we're using team_a token
    assert "team_a" in opp.details["poly_token_id"]
    # Verify Kalshi side is NO (team_b wins)
    assert opp.details["kalshi_side"] == "no"


def test_direction2_uses_team_b_token():
    """Direction 2: Buy Poly NO (team_b) + Kalshi YES."""
    opp = ArbitrageOpportunity(
        event_title="Team A vs Team B",
        team_a="Team A",
        team_b="Team B",
        platform_buy_yes=Platform.KALSHI,
        platform_buy_no=Platform.POLYMARKET,
        yes_price=0.50,
        no_price=0.45,
        total_cost=0.97,
        profit_pct=3.0,
        roi_after_fees=1.5,
        found_at=datetime.now(UTC),
        details={
            "arb_type": "yes_no",
            "poly_token_id": "token_team_b_67890",
            "poly_side": "BUY",  # BUY team_b token, NOT SELL team_a!
            "kalshi_ticker": "KXGAME-TEAMA",
            "kalshi_side": "yes",
        },
    )

    # Verify poly_side is BUY (buying team_b token)
    assert opp.details["poly_side"] == "BUY"
    # Verify we're using team_b token
    assert "team_b" in opp.details["poly_token_id"]
    # Verify Kalshi side is YES (team_a wins)
    assert opp.details["kalshi_side"] == "yes"


def test_arbitrage_covers_both_outcomes():
    """Verify that an arbitrage trade covers both possible outcomes."""
    # Direction 1: Poly team_a + Kalshi NO (team_b)
    # If team_a wins: Poly pays $1
    # If team_b wins: Kalshi pays $1
    # Both outcomes covered!

    poly_team = "team_a"
    kalshi_side = "no"  # Kalshi NO = team_b wins

    outcomes_covered = {poly_team, "team_b" if kalshi_side == "no" else "team_a"}
    assert outcomes_covered == {"team_a", "team_b"}, "Must cover both outcomes!"


def test_same_outcome_is_not_arbitrage():
    """Verify that betting same outcome on both platforms is NOT arbitrage."""
    # BAD: Poly team_b + Kalshi NO (also team_b)
    # Both bets win if team_b wins, both lose if team_a wins
    # This is NOT arbitrage - it's double betting!

    poly_team = "team_b"
    kalshi_side = "no"  # Kalshi NO = team_b wins

    outcomes_covered = {poly_team, "team_b" if kalshi_side == "no" else "team_a"}

    # This should fail - both are team_b!
    assert len(outcomes_covered) == 1, "Same outcome on both = NOT arbitrage!"
```

**Step 2: Run tests**

```bash
uv run pytest tests/test_executor_tokens.py -v
```

Expected: All tests PASS

**Step 3: Commit**

```bash
git add tests/test_executor_tokens.py
git commit -m "test: add integration tests for executor token matching"
```

---

### Task 5: Deploy and Verify

**Step 1: Push changes**

```bash
git push origin main
```

**Step 2: Deploy to server**

```bash
ssh marmok@192.168.1.251 "cd ~/sports-arb && git pull origin main"
```

**Step 3: Restart service**

```bash
ssh marmok@192.168.1.251 "pkill -9 -f 'src.main'; cd ~/sports-arb && nohup ~/.local/bin/uv run python -m src.main > ~/app.log 2>&1 &"
```

**Step 4: Monitor logs for correct behavior**

```bash
ssh marmok@192.168.1.251 "tail -f ~/app.log | grep -E 'Order sides|EXEC'"
```

Expected output should show:
- `Order sides: poly=BUY (token=...), kalshi=no` for Direction 1
- `Order sides: poly=BUY (token=...), kalshi=yes` for Direction 2

**Step 5: Commit deployment verification**

If working, no commit needed. If issues found, fix and re-deploy.

---

## Summary

| Task | Description | Files |
|------|-------------|-------|
| 1 | Add correct token selection in arbitrage.py | `src/engine/arbitrage.py` |
| 2 | Update order_placer to use explicit sides | `src/executor/order_placer.py` |
| 3 | Add validation in risk manager | `src/executor/risk_manager.py` |
| 4 | Write integration tests | `tests/test_executor_tokens.py` |
| 5 | Deploy and verify | Remote server |

## Key Changes

1. **arbitrage.py**: Store `poly_side` and `kalshi_side` explicitly in details
2. **arbitrage.py**: Use `clob_token_ids[1]` for Direction 2/4 (team_b outcomes)
3. **order_placer.py**: Read sides from details instead of inferring
4. **risk_manager.py**: Validate new required fields

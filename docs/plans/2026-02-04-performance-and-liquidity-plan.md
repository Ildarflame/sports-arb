# Performance Optimization & Liquidity Analysis Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce scan time from 7s to ~4s and add liquidity analysis to show max executable size for each arbitrage opportunity.

**Current State:**
- Scan loop takes 6-8s (fits 10s cycle but tight)
- Order book depth data available from Polymarket but discarded
- No visibility into how much money can be executed at arbitrage prices

---

## Section 1: Quick Wins (Performance)

### Task 1.1: Increase Concurrency Limit

**File:** `src/main.py:35`

**Current:**
```python
_price_semaphore = asyncio.Semaphore(15)
```

**Change to:**
```python
_price_semaphore = asyncio.Semaphore(25)
```

**Expected gain:** 500ms-1s on book fetches

**Verification:** Run scan, check logs for "Book fetch" timing

---

### Task 1.2: Cache Matched Events

**Problem:** Matcher runs full fuzzy matching every 10s scan, even when markets haven't changed.

**File:** `src/main.py` and `src/state.py`

**Implementation:**
1. Add to `src/state.py`:
```python
"matched_events_cache": {},      # (poly_id, kalshi_id) -> SportEvent
"matched_events_cache_time": 0,  # When cache was built
```

2. In `src/main.py:497`, wrap `match_events()`:
```python
MATCH_CACHE_TTL = 300  # 5 minutes

# Check if we can use cached matches
match_cache_age = now - app_state["matched_events_cache_time"]
if match_cache_age < MATCH_CACHE_TTL and app_state["matched_events_cache"]:
    # Reuse cached matches, just update prices
    matched = list(app_state["matched_events_cache"].values())
    logger.info(f"Using cached matches ({len(matched)} events)")
else:
    # Full re-match
    matched = match_events(poly_markets, kalshi_markets)
    app_state["matched_events_cache"] = {
        (e.markets.get(Platform.POLYMARKET).market_id if e.markets.get(Platform.POLYMARKET) else "",
         e.markets.get(Platform.KALSHI).market_id if e.markets.get(Platform.KALSHI) else ""): e
        for e in matched if e.matched
    }
    app_state["matched_events_cache_time"] = now
```

**Expected gain:** 500ms-1s when cache hits

**Verification:** Check logs for "Using cached matches"

---

### Task 1.3: Optimize Polymarket Token ID Lookup

**Problem:** Per-market CLOB token ID extraction repeated multiple times.

**File:** `src/main.py:89-96`, `src/main.py:170-176`

**Implementation:** Extract token ID once per market during initial fetch, store in `raw_data["primary_token_id"]`.

In `src/connectors/polymarket.py`, after market creation:
```python
# Store primary token for quick access
token_ids = market.raw_data.get("clob_token_ids", [])
market.raw_data["primary_token_id"] = token_ids[0] if token_ids else market.market_id
```

Then in main.py, replace:
```python
token_ids = pm.raw_data.get("clob_token_ids", [])
token_id = token_ids[0] if token_ids else pm.market_id
```
With:
```python
token_id = pm.raw_data.get("primary_token_id", pm.market_id)
```

**Expected gain:** 100-200ms (reduces dict lookups)

---

## Section 2: Liquidity Data Structures

### Task 2.1: Add OrderBookLevel Model

**File:** `src/models.py`

**Add after MarketPrice class:**
```python
class OrderBookLevel(BaseModel):
    """Single level in order book."""
    price: float
    size: float  # Number of contracts available


class OrderBookDepth(BaseModel):
    """Full order book depth for a market."""
    bids: list[OrderBookLevel] = []  # Sorted by price descending (best first)
    asks: list[OrderBookLevel] = []  # Sorted by price ascending (best first)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread_pct(self) -> float | None:
        if self.best_bid and self.best_ask and self.best_ask > 0:
            return ((self.best_ask - self.best_bid) / self.best_ask) * 100
        return None

    def volume_at_price(self, side: str, max_price: float) -> float:
        """Total volume available at or better than max_price."""
        levels = self.asks if side == "buy" else self.bids
        total = 0.0
        for level in levels:
            if side == "buy" and level.price <= max_price:
                total += level.size
            elif side == "sell" and level.price >= max_price:
                total += level.size
            else:
                break  # Levels are sorted, so we can stop
        return total

    def cost_to_fill(self, side: str, size: float) -> tuple[float, float]:
        """Calculate cost to fill `size` contracts.

        Returns (total_cost, average_price).
        If insufficient liquidity, returns cost for available amount.
        """
        levels = self.asks if side == "buy" else self.bids
        remaining = size
        total_cost = 0.0
        filled = 0.0

        for level in levels:
            if remaining <= 0:
                break
            fill_at_level = min(remaining, level.size)
            total_cost += fill_at_level * level.price
            filled += fill_at_level
            remaining -= fill_at_level

        avg_price = total_cost / filled if filled > 0 else 0
        return total_cost, avg_price
```

---

### Task 2.2: Add Depth to MarketPrice

**File:** `src/models.py`

**Modify MarketPrice:**
```python
class MarketPrice(BaseModel):
    yes_price: float
    no_price: float
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    volume: float = 0
    last_updated: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # NEW: Order book depth
    yes_depth: OrderBookDepth | None = None
    no_depth: OrderBookDepth | None = None
```

---

### Task 2.3: Update Polymarket Book Fetch

**File:** `src/connectors/polymarket.py:784-832`

**Current:** Extracts only best bid/ask, discards depth.

**New implementation:**
```python
async def fetch_book(self, token_id: str) -> MarketPrice | None:
    """Fetch order book with full depth for a token."""
    url = f"{self._clob_base}/book"
    params = {"token_id": token_id}

    async with self._session.get(url, params=params) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()

    raw_bids = data.get("bids", [])
    raw_asks = data.get("asks", [])

    # Parse into OrderBookLevel objects
    from src.models import OrderBookLevel, OrderBookDepth

    bids = sorted(
        [OrderBookLevel(price=float(b["price"]), size=float(b["size"])) for b in raw_bids],
        key=lambda x: -x.price  # Best (highest) first
    )
    asks = sorted(
        [OrderBookLevel(price=float(a["price"]), size=float(a["size"])) for a in raw_asks],
        key=lambda x: x.price  # Best (lowest) first
    )

    yes_depth = OrderBookDepth(bids=bids, asks=asks)

    # Derive NO depth by inverting prices
    no_bids = [OrderBookLevel(price=1.0 - a.price, size=a.size) for a in asks]
    no_asks = [OrderBookLevel(price=1.0 - b.price, size=b.size) for b in bids]
    no_depth = OrderBookDepth(bids=no_bids, asks=no_asks)

    best_bid = yes_depth.best_bid
    best_ask = yes_depth.best_ask

    # Reject junk books (spread > 90%)
    if best_bid and best_ask:
        spread = (best_ask - best_bid) / best_ask if best_ask > 0 else 1
        if spread > 0.9:
            return None

    # Calculate midpoint
    if best_bid and best_ask:
        yes_price = (best_bid + best_ask) / 2
    elif best_bid:
        yes_price = best_bid
    elif best_ask:
        yes_price = best_ask
    else:
        # Fallback to midpoint API
        return await self._fetch_midpoint_fallback(token_id)

    return MarketPrice(
        yes_price=yes_price,
        no_price=1.0 - yes_price,
        yes_bid=best_bid,
        yes_ask=best_ask,
        no_bid=1.0 - best_ask if best_ask else None,
        no_ask=1.0 - best_bid if best_bid else None,
        yes_depth=yes_depth,
        no_depth=no_depth,
    )
```

---

## Section 3: Liquidity Analysis

### Task 3.1: Create Liquidity Analyzer

**File:** `src/engine/liquidity.py` (NEW)

```python
"""Liquidity analysis for arbitrage opportunities."""

from __future__ import annotations

from dataclasses import dataclass
from src.models import MarketPrice, OrderBookDepth, Platform, SportEvent


@dataclass
class LiquidityAnalysis:
    """Liquidity profile for an arbitrage opportunity."""

    # Max executable at current best prices (no slippage)
    max_size_at_best: float

    # Max executable with up to X% slippage
    max_size_1pct_slip: float
    max_size_2pct_slip: float
    max_size_5pct_slip: float

    # Dollar amounts (assuming $1 per contract payout)
    max_dollars_at_best: float
    max_dollars_1pct_slip: float
    max_dollars_2pct_slip: float

    # Per-platform breakdown
    poly_liquidity: float  # Contracts available on Poly side
    kalshi_liquidity: float  # Contracts available on Kalshi side

    # Limiting factor
    bottleneck: str  # "polymarket" or "kalshi"

    # Quality score (0-100)
    liquidity_score: float


def analyze_liquidity(
    event: SportEvent,
    buy_yes_platform: Platform,
    yes_price: float,
    no_price: float,
) -> LiquidityAnalysis | None:
    """Analyze liquidity for an arbitrage trade.

    Args:
        event: SportEvent with market data
        buy_yes_platform: Platform where we buy YES
        yes_price: Target YES price
        no_price: Target NO price

    Returns:
        LiquidityAnalysis or None if insufficient data
    """
    poly_market = event.markets.get(Platform.POLYMARKET)
    kalshi_market = event.markets.get(Platform.KALSHI)

    if not poly_market or not kalshi_market:
        return None

    poly_price = poly_market.price
    kalshi_price = kalshi_market.price

    if not poly_price or not kalshi_price:
        return None

    # Determine which side we're buying on each platform
    if buy_yes_platform == Platform.POLYMARKET:
        # Buy YES on Poly (use asks), Buy NO on Kalshi
        poly_depth = poly_price.yes_depth
        poly_side = "buy"  # We're buying from asks
        # Kalshi doesn't have depth, estimate from volume
        kalshi_liquidity = _estimate_kalshi_liquidity(kalshi_price)
    else:
        # Buy YES on Kalshi, Buy NO on Poly (use NO asks = inverted YES bids)
        poly_depth = poly_price.no_depth
        poly_side = "buy"
        kalshi_liquidity = _estimate_kalshi_liquidity(kalshi_price)

    # Calculate Polymarket liquidity at various slippage levels
    if poly_depth:
        poly_at_best = poly_depth.asks[0].size if poly_depth.asks else 0

        # 1% slippage = willing to pay up to 1% more
        target_1pct = yes_price * 1.01 if buy_yes_platform == Platform.POLYMARKET else no_price * 1.01
        poly_1pct = poly_depth.volume_at_price("buy", target_1pct)

        target_2pct = yes_price * 1.02 if buy_yes_platform == Platform.POLYMARKET else no_price * 1.02
        poly_2pct = poly_depth.volume_at_price("buy", target_2pct)

        target_5pct = yes_price * 1.05 if buy_yes_platform == Platform.POLYMARKET else no_price * 1.05
        poly_5pct = poly_depth.volume_at_price("buy", target_5pct)
    else:
        # No depth data, estimate from volume
        poly_at_best = (poly_price.volume or 0) * 0.01  # Rough: 1% of volume
        poly_1pct = poly_at_best * 2
        poly_2pct = poly_at_best * 3
        poly_5pct = poly_at_best * 5

    # Bottleneck is minimum of both platforms
    max_at_best = min(poly_at_best, kalshi_liquidity)
    max_1pct = min(poly_1pct, kalshi_liquidity * 1.5)
    max_2pct = min(poly_2pct, kalshi_liquidity * 2)
    max_5pct = min(poly_5pct, kalshi_liquidity * 3)

    bottleneck = "polymarket" if poly_at_best < kalshi_liquidity else "kalshi"

    # Liquidity score: 0-100 based on max executable
    # $100 = score 20, $500 = score 50, $1000+ = score 80+
    score = min(100, (max_at_best / 10) + 10)

    # Convert contracts to dollars (assuming ~$0.50 avg price)
    avg_price = (yes_price + no_price) / 2

    return LiquidityAnalysis(
        max_size_at_best=max_at_best,
        max_size_1pct_slip=max_1pct,
        max_size_2pct_slip=max_2pct,
        max_size_5pct_slip=max_5pct,
        max_dollars_at_best=max_at_best * avg_price,
        max_dollars_1pct_slip=max_1pct * avg_price,
        max_dollars_2pct_slip=max_2pct * avg_price,
        poly_liquidity=poly_at_best,
        kalshi_liquidity=kalshi_liquidity,
        bottleneck=bottleneck,
        liquidity_score=score,
    )


def _estimate_kalshi_liquidity(price: MarketPrice) -> float:
    """Estimate Kalshi liquidity from volume (no depth data available).

    Kalshi API doesn't expose order book depth, only best bid/ask.
    We estimate based on total volume as a rough proxy.
    """
    volume = price.volume or 0
    # Heuristic: ~2% of daily volume is typically available at best price
    # This is a rough estimate - actual may vary significantly
    return volume * 0.02
```

---

### Task 3.2: Add Liquidity to ArbitrageOpportunity

**File:** `src/engine/arbitrage.py`

**In `calculate_arbitrage()`, after ROI calculation (~line 560), add:**
```python
from src.engine.liquidity import analyze_liquidity, LiquidityAnalysis

# Analyze liquidity
liquidity = analyze_liquidity(
    event=event,
    buy_yes_platform=best_opp.platform_buy_yes,
    yes_price=best_opp.yes_price,
    no_price=best_opp.no_price,
)

if liquidity:
    best_opp.details["liquidity"] = {
        "max_at_best": round(liquidity.max_dollars_at_best, 2),
        "max_1pct_slip": round(liquidity.max_dollars_1pct_slip, 2),
        "max_2pct_slip": round(liquidity.max_dollars_2pct_slip, 2),
        "bottleneck": liquidity.bottleneck,
        "score": round(liquidity.liquidity_score, 1),
        "poly_contracts": round(liquidity.poly_liquidity, 0),
        "kalshi_contracts": round(liquidity.kalshi_liquidity, 0),
    }
```

---

### Task 3.3: Display Liquidity in UI

**File:** `src/web/templates/partials/alerts.html`

**Add after ROI display in summary:**
```html
{% set liq = d.get('liquidity', {}) %}
{% if liq %}
    <span class="arb-liquidity" title="Max executable: ${{ liq.max_at_best }} at best, ${{ liq.max_1pct_slip }} with 1% slip ({{ liq.bottleneck }} limiting)">
        {% if liq.max_at_best >= 500 %}
            <span class="micro-badge mb-vol-high">LIQ ${{ "%.0f"|format(liq.max_at_best) }}</span>
        {% elif liq.max_at_best >= 100 %}
            <span class="micro-badge mb-conf-med">LIQ ${{ "%.0f"|format(liq.max_at_best) }}</span>
        {% else %}
            <span class="micro-badge mb-conf-low">LIQ ${{ "%.0f"|format(liq.max_at_best) }}</span>
        {% endif %}
    </span>
{% endif %}
```

**In expanded details section, add liquidity breakdown:**
```html
{% if liq %}
<div style="margin-top: 0.5rem; padding: 0.5rem; background: rgba(59,130,246,0.08); border-radius: 4px;">
    <div style="font-size: 0.72rem; font-weight: 600; color: var(--blue); margin-bottom: 0.3rem;">Liquidity Analysis</div>
    <div style="font-size: 0.75rem; line-height: 1.6;">
        Max @ best price: <strong>${{ "%.0f"|format(liq.max_at_best) }}</strong><br>
        Max @ 1% slippage: <strong>${{ "%.0f"|format(liq.max_1pct_slip) }}</strong><br>
        Max @ 2% slippage: <strong>${{ "%.0f"|format(liq.max_2pct_slip) }}</strong><br>
        Bottleneck: <strong>{{ liq.bottleneck|capitalize }}</strong>
        (Poly: {{ liq.poly_contracts }} contracts, Kalshi: {{ liq.kalshi_contracts }} contracts)
    </div>
</div>
{% endif %}
```

---

## Section 4: Executor Integration

### Task 4.1: Validate Liquidity Before Execution

**File:** `src/executor/risk_manager.py`

**Add new check after confidence check (~line 115):**
```python
# 8. Liquidity check - ensure enough liquidity for bet size
liquidity = opp.details.get("liquidity", {})
max_executable = liquidity.get("max_at_best", 0)
if max_executable > 0 and max_executable < self.min_bet:
    return RiskCheckResult(
        False,
        f"Insufficient liquidity: ${max_executable:.0f} available, need ${self.min_bet:.0f}"
    )
```

### Task 4.2: Dynamic Bet Sizing Based on Liquidity

**File:** `src/executor/risk_manager.py`

**Modify `calculate_bet_size()` to respect liquidity:**
```python
def calculate_bet_size(
    self,
    opp: ArbitrageOpportunity,
    poly_balance: float,
    kalshi_balance: float,
) -> float:
    """Calculate optimal bet size within limits and liquidity."""
    # Can't bet more than we have on either platform
    max_by_balance = min(poly_balance, kalshi_balance)

    # Can't bet more than available liquidity
    liquidity = opp.details.get("liquidity", {})
    max_by_liquidity = liquidity.get("max_1pct_slip", float("inf"))

    # Apply all limits
    bet = min(self.max_bet, max_by_balance, max_by_liquidity)
    bet = max(bet, self.min_bet)

    # Final sanity check
    if bet > max_by_balance or bet > max_by_liquidity:
        return 0  # Can't execute

    return round(bet, 2)
```

---

## Section 5: Testing

### Task 5.1: Unit Tests for Liquidity

**File:** `tests/test_liquidity.py` (NEW)

```python
import pytest
from src.models import OrderBookLevel, OrderBookDepth, MarketPrice


def test_order_book_depth_best_prices():
    depth = OrderBookDepth(
        bids=[
            OrderBookLevel(price=0.50, size=100),
            OrderBookLevel(price=0.49, size=200),
        ],
        asks=[
            OrderBookLevel(price=0.52, size=150),
            OrderBookLevel(price=0.53, size=300),
        ],
    )
    assert depth.best_bid == 0.50
    assert depth.best_ask == 0.52
    assert depth.spread_pct == pytest.approx(3.85, rel=0.1)


def test_volume_at_price():
    depth = OrderBookDepth(
        bids=[],
        asks=[
            OrderBookLevel(price=0.52, size=100),
            OrderBookLevel(price=0.53, size=200),
            OrderBookLevel(price=0.55, size=500),
        ],
    )
    # Buy up to 0.53 = 100 + 200 = 300
    assert depth.volume_at_price("buy", 0.53) == 300
    # Buy up to 0.52 = 100
    assert depth.volume_at_price("buy", 0.52) == 100


def test_cost_to_fill():
    depth = OrderBookDepth(
        bids=[],
        asks=[
            OrderBookLevel(price=0.50, size=100),
            OrderBookLevel(price=0.52, size=200),
        ],
    )
    # Fill 150 contracts: 100 @ 0.50 + 50 @ 0.52 = 50 + 26 = 76
    cost, avg = depth.cost_to_fill("buy", 150)
    assert cost == pytest.approx(76.0, rel=0.01)
    assert avg == pytest.approx(0.507, rel=0.01)
```

---

## Implementation Order

1. **Task 2.1-2.2**: Add data models (no behavior change)
2. **Task 2.3**: Update Polymarket book fetch (stores depth)
3. **Task 3.1**: Create liquidity analyzer
4. **Task 3.2**: Integrate into arbitrage calculation
5. **Task 1.1-1.3**: Performance optimizations
6. **Task 3.3**: UI updates
7. **Task 4.1-4.2**: Executor integration
8. **Task 5.1**: Tests

---

## Verification

After implementation:
1. Run `uv run pytest tests/ -v` - all tests pass
2. Run app locally, check scan time in logs (target: <5s)
3. Check arbitrage cards show "LIQ $XXX" badge
4. Expand card, verify liquidity breakdown shows
5. Test executor with small bet, verify liquidity validation works

---

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Kalshi has no depth data | Use volume-based estimation, clearly mark as estimate |
| Depth data increases memory | Limit to top 10 levels per side |
| API rate limits | Already semaphore-limited, depth comes with book fetch |
| Stale depth data | Refresh with each book fetch (every candidate scan) |

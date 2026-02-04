# Auto Executor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement fully automatic arbitrage execution system that places trades on Polymarket and Kalshi when opportunities are detected.

**Architecture:** Modular executor with risk manager (pre-trade checks), order placer (parallel execution on both platforms), position manager (track open positions), and Telegram notifier (real-time alerts). Integrates with existing arbitrage calculator.

**Tech Stack:** Python 3.13, asyncio, httpx, py-clob-client (Polymarket), python-telegram-bot, aiosqlite

---

## Task 1: Add Dependencies

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add new dependencies**

```bash
uv add py-clob-client python-telegram-bot
```

**Step 2: Verify installation**

Run: `uv run python -c "from py_clob_client.client import ClobClient; from telegram import Bot; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add py-clob-client and python-telegram-bot dependencies"
```

---

## Task 2: Extend Configuration

**Files:**
- Modify: `src/config.py`

**Step 1: Write the failing test**

Create: `tests/test_config.py`

```python
"""Tests for executor configuration."""

from src.config import Settings


def test_executor_settings_exist():
    """Verify executor settings are defined with defaults."""
    s = Settings()

    # Polymarket trading
    assert hasattr(s, "poly_private_key")
    assert hasattr(s, "poly_funder_address")

    # Telegram
    assert hasattr(s, "telegram_bot_token")
    assert hasattr(s, "telegram_chat_id")

    # Executor limits
    assert s.executor_enabled == False  # Disabled by default for safety
    assert s.executor_min_bet == 1.0
    assert s.executor_max_bet == 2.0
    assert s.executor_min_roi == 1.0
    assert s.executor_max_daily_trades == 50
    assert s.executor_max_daily_loss == 5.0
    assert s.executor_min_platform_balance == 1.0
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with AttributeError

**Step 3: Add executor settings to config**

```python
# In src/config.py, add to Settings class:

    # Polymarket trading
    poly_private_key: str = ""
    poly_funder_address: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Executor settings
    executor_enabled: bool = False  # Must explicitly enable
    executor_min_bet: float = 1.0
    executor_max_bet: float = 2.0
    executor_min_roi: float = 1.0
    executor_max_daily_trades: int = 50
    executor_max_daily_loss: float = 5.0
    executor_min_platform_balance: float = 1.0
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat(config): add executor and telegram settings"
```

---

## Task 3: Create Executor Models

**Files:**
- Create: `src/executor/__init__.py`
- Create: `src/executor/models.py`

**Step 1: Write the failing test**

Create: `tests/test_executor_models.py`

```python
"""Tests for executor data models."""

from datetime import datetime, UTC

from src.executor.models import (
    ExecutionResult,
    ExecutionStatus,
    LegResult,
    OpenPosition,
    RiskCheckResult,
)


def test_leg_result_success():
    """LegResult can represent successful order."""
    leg = LegResult(
        platform="polymarket",
        success=True,
        order_id="abc123",
        filled_amount=1.5,
        filled_price=0.52,
        error=None,
    )
    assert leg.success
    assert leg.order_id == "abc123"


def test_leg_result_failure():
    """LegResult can represent failed order."""
    leg = LegResult(
        platform="kalshi",
        success=False,
        order_id=None,
        filled_amount=0,
        filled_price=0,
        error="Insufficient balance",
    )
    assert not leg.success
    assert leg.error == "Insufficient balance"


def test_execution_result_both_filled():
    """ExecutionResult determines SUCCESS when both legs fill."""
    poly = LegResult("polymarket", True, "p1", 1.0, 0.51, None)
    kalshi = LegResult("kalshi", True, "k1", 1.0, 0.48, None)

    result = ExecutionResult(poly_leg=poly, kalshi_leg=kalshi)
    assert result.status == ExecutionStatus.SUCCESS


def test_execution_result_partial():
    """ExecutionResult determines PARTIAL when one leg fails."""
    poly = LegResult("polymarket", True, "p1", 1.0, 0.51, None)
    kalshi = LegResult("kalshi", False, None, 0, 0, "Failed")

    result = ExecutionResult(poly_leg=poly, kalshi_leg=kalshi)
    assert result.status == ExecutionStatus.PARTIAL


def test_execution_result_both_failed():
    """ExecutionResult determines FAILED when both legs fail."""
    poly = LegResult("polymarket", False, None, 0, 0, "Error 1")
    kalshi = LegResult("kalshi", False, None, 0, 0, "Error 2")

    result = ExecutionResult(poly_leg=poly, kalshi_leg=kalshi)
    assert result.status == ExecutionStatus.FAILED


def test_open_position():
    """OpenPosition tracks arbitrage position."""
    pos = OpenPosition(
        id="pos_123",
        event_title="Lakers vs Celtics",
        team_a="Lakers",
        team_b="Celtics",
        poly_side="YES",
        poly_amount=1.02,
        poly_contracts=2.0,
        poly_avg_price=0.51,
        kalshi_side="no",
        kalshi_amount=0.98,
        kalshi_contracts=2,
        kalshi_avg_price=0.49,
        arb_type="yes_no",
        expected_roi=2.1,
        opened_at=datetime.now(UTC),
        status="open",
    )
    assert pos.status == "open"
    assert pos.poly_side == "YES"


def test_risk_check_result():
    """RiskCheckResult indicates pass/fail with reason."""
    passed = RiskCheckResult(passed=True, reason=None)
    assert passed.passed

    failed = RiskCheckResult(passed=False, reason="Daily loss limit reached")
    assert not failed.passed
    assert failed.reason == "Daily loss limit reached"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_executor_models.py -v`
Expected: FAIL with ModuleNotFoundError

**Step 3: Create executor models**

Create `src/executor/__init__.py`:
```python
"""Executor module for automatic arbitrage execution."""
```

Create `src/executor/models.py`:
```python
"""Data models for executor module."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ExecutionStatus(Enum):
    """Status of arbitrage execution."""
    SUCCESS = "success"      # Both legs filled
    PARTIAL = "partial"      # One leg filled, one failed
    FAILED = "failed"        # Both legs failed


@dataclass
class LegResult:
    """Result of executing one leg of the arbitrage."""
    platform: str           # "polymarket" or "kalshi"
    success: bool
    order_id: str | None
    filled_amount: float    # Dollar amount filled
    filled_price: float     # Average fill price
    error: str | None


@dataclass
class ExecutionResult:
    """Result of executing full arbitrage (both legs)."""
    poly_leg: LegResult
    kalshi_leg: LegResult
    executed_at: datetime = field(default_factory=lambda: datetime.now(__import__('datetime').UTC))

    @property
    def status(self) -> ExecutionStatus:
        """Determine execution status from leg results."""
        if self.poly_leg.success and self.kalshi_leg.success:
            return ExecutionStatus.SUCCESS
        elif self.poly_leg.success or self.kalshi_leg.success:
            return ExecutionStatus.PARTIAL
        else:
            return ExecutionStatus.FAILED


@dataclass
class OpenPosition:
    """Tracks an open arbitrage position until settlement."""
    id: str
    event_title: str
    team_a: str
    team_b: str

    # Polymarket leg
    poly_side: str          # "YES" or "NO"
    poly_amount: float      # Dollar amount spent
    poly_contracts: float   # Number of contracts
    poly_avg_price: float
    poly_order_id: str = ""

    # Kalshi leg
    kalshi_side: str        # "yes" or "no"
    kalshi_amount: float
    kalshi_contracts: int
    kalshi_avg_price: float
    kalshi_order_id: str = ""

    # Metadata
    arb_type: str           # "yes_no", "cross_team", "3way"
    expected_roi: float
    opened_at: datetime = field(default_factory=lambda: datetime.now(__import__('datetime').UTC))
    status: str = "open"    # "open", "settled", "partial"

    # Settlement (filled after match ends)
    settled_at: datetime | None = None
    actual_pnl: float | None = None
    winning_side: str | None = None


@dataclass
class RiskCheckResult:
    """Result of pre-trade risk check."""
    passed: bool
    reason: str | None = None
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_executor_models.py -v`
Expected: PASS (6 tests)

**Step 5: Commit**

```bash
git add src/executor/ tests/test_executor_models.py
git commit -m "feat(executor): add data models for execution tracking"
```

---

## Task 4: Create Risk Manager

**Files:**
- Create: `src/executor/risk_manager.py`
- Create: `tests/test_risk_manager.py`

**Step 1: Write the failing test**

Create: `tests/test_risk_manager.py`

```python
"""Tests for risk manager."""

import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, UTC

from src.executor.risk_manager import RiskManager
from src.models import ArbitrageOpportunity, Platform


@pytest.fixture
def risk_manager():
    """Create risk manager with test settings."""
    return RiskManager(
        min_bet=1.0,
        max_bet=2.0,
        min_roi=1.0,
        max_roi=50.0,
        max_daily_trades=50,
        max_daily_loss=5.0,
        min_platform_balance=1.0,
    )


@pytest.fixture
def sample_opportunity():
    """Create sample arbitrage opportunity."""
    return ArbitrageOpportunity(
        event_title="Lakers vs Celtics",
        team_a="Lakers",
        team_b="Celtics",
        platform_buy_yes=Platform.POLYMARKET,
        platform_buy_no=Platform.KALSHI,
        yes_price=0.51,
        no_price=0.48,
        total_cost=0.99,
        profit_pct=1.0,
        roi_after_fees=2.5,
        found_at=datetime.now(UTC),
        details={
            "executable": True,
            "confidence": "high",
            "spread_pct": 5.0,
        },
    )


def test_kill_switch_blocks_trading(risk_manager, sample_opportunity):
    """Kill switch should block all trades."""
    risk_manager.enabled = False
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=10, kalshi_balance=10)
    assert not result.passed
    assert "kill switch" in result.reason.lower()


def test_insufficient_balance_poly(risk_manager, sample_opportunity):
    """Should reject if Polymarket balance too low."""
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=0.5, kalshi_balance=10)
    assert not result.passed
    assert "polymarket balance" in result.reason.lower()


def test_insufficient_balance_kalshi(risk_manager, sample_opportunity):
    """Should reject if Kalshi balance too low."""
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=10, kalshi_balance=0.5)
    assert not result.passed
    assert "kalshi balance" in result.reason.lower()


def test_roi_too_low(risk_manager, sample_opportunity):
    """Should reject if ROI below minimum."""
    sample_opportunity.roi_after_fees = 0.5
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=10, kalshi_balance=10)
    assert not result.passed
    assert "roi" in result.reason.lower()


def test_roi_too_high_suspicious(risk_manager, sample_opportunity):
    """Should reject suspiciously high ROI."""
    sample_opportunity.roi_after_fees = 75.0
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=10, kalshi_balance=10)
    assert not result.passed
    assert "suspicious" in result.reason.lower()


def test_daily_trade_limit(risk_manager, sample_opportunity):
    """Should reject when daily trade limit reached."""
    risk_manager._daily_trades = 50
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=10, kalshi_balance=10)
    assert not result.passed
    assert "daily" in result.reason.lower()


def test_daily_loss_limit(risk_manager, sample_opportunity):
    """Should reject when daily loss limit reached."""
    risk_manager._daily_pnl = -5.5
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=10, kalshi_balance=10)
    assert not result.passed
    assert "loss" in result.reason.lower()


def test_valid_opportunity_passes(risk_manager, sample_opportunity):
    """Valid opportunity should pass all checks."""
    result = risk_manager.check_opportunity(sample_opportunity, poly_balance=10, kalshi_balance=10)
    assert result.passed
    assert result.reason is None


def test_calculate_bet_size(risk_manager, sample_opportunity):
    """Should calculate appropriate bet size."""
    bet = risk_manager.calculate_bet_size(sample_opportunity, poly_balance=10, kalshi_balance=10)
    assert risk_manager.min_bet <= bet <= risk_manager.max_bet


def test_calculate_bet_size_respects_balance(risk_manager, sample_opportunity):
    """Bet size should not exceed available balance."""
    bet = risk_manager.calculate_bet_size(sample_opportunity, poly_balance=1.5, kalshi_balance=10)
    assert bet <= 1.5
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_risk_manager.py -v`
Expected: FAIL with ModuleNotFoundError

**Step 3: Implement risk manager**

Create `src/executor/risk_manager.py`:

```python
"""Risk manager for pre-trade validation."""

from __future__ import annotations

import logging
from datetime import date

from src.executor.models import RiskCheckResult
from src.models import ArbitrageOpportunity

logger = logging.getLogger(__name__)


class RiskManager:
    """Validates opportunities against risk limits before execution."""

    def __init__(
        self,
        min_bet: float = 1.0,
        max_bet: float = 2.0,
        min_roi: float = 1.0,
        max_roi: float = 50.0,
        max_daily_trades: int = 50,
        max_daily_loss: float = 5.0,
        min_platform_balance: float = 1.0,
    ):
        self.min_bet = min_bet
        self.max_bet = max_bet
        self.min_roi = min_roi
        self.max_roi = max_roi
        self.max_daily_trades = max_daily_trades
        self.max_daily_loss = max_daily_loss
        self.min_platform_balance = min_platform_balance

        # Runtime state
        self.enabled = True
        self._daily_trades = 0
        self._daily_pnl = 0.0
        self._current_date = date.today()
        self._open_positions: set[str] = set()  # event keys with open positions

    def _reset_daily_if_needed(self) -> None:
        """Reset daily counters if date changed."""
        today = date.today()
        if today != self._current_date:
            self._daily_trades = 0
            self._daily_pnl = 0.0
            self._current_date = today
            logger.info("Daily counters reset for new day")

    def check_opportunity(
        self,
        opp: ArbitrageOpportunity,
        poly_balance: float,
        kalshi_balance: float,
    ) -> RiskCheckResult:
        """Run all risk checks on opportunity.

        Returns RiskCheckResult with passed=True if all checks pass,
        or passed=False with reason describing first failed check.
        """
        self._reset_daily_if_needed()

        # 1. Kill switch
        if not self.enabled:
            return RiskCheckResult(False, "Kill switch is OFF - trading disabled")

        # 2. Balance checks
        if poly_balance < self.min_platform_balance:
            return RiskCheckResult(False, f"Polymarket balance too low: ${poly_balance:.2f}")
        if kalshi_balance < self.min_platform_balance:
            return RiskCheckResult(False, f"Kalshi balance too low: ${kalshi_balance:.2f}")

        # 3. ROI checks
        if opp.roi_after_fees < self.min_roi:
            return RiskCheckResult(False, f"ROI too low: {opp.roi_after_fees:.2f}% < {self.min_roi}%")
        if opp.roi_after_fees > self.max_roi:
            return RiskCheckResult(False, f"Suspicious ROI: {opp.roi_after_fees:.2f}% > {self.max_roi}%")

        # 4. Daily limits
        if self._daily_trades >= self.max_daily_trades:
            return RiskCheckResult(False, f"Daily trade limit reached: {self._daily_trades}/{self.max_daily_trades}")
        if self._daily_pnl <= -self.max_daily_loss:
            return RiskCheckResult(False, f"Daily loss limit reached: ${abs(self._daily_pnl):.2f}")

        # 5. Duplicate position check
        event_key = f"{opp.team_a}:{opp.team_b}".lower()
        if event_key in self._open_positions:
            return RiskCheckResult(False, f"Already have open position on {opp.event_title}")

        # 6. Confidence check for live arbs
        if opp.details.get("is_live"):
            if not opp.details.get("executable"):
                return RiskCheckResult(False, "Live arb requires executable bid/ask prices")
            if opp.details.get("confidence") != "high":
                return RiskCheckResult(False, "Live arb requires high confidence")

        return RiskCheckResult(True, None)

    def calculate_bet_size(
        self,
        opp: ArbitrageOpportunity,
        poly_balance: float,
        kalshi_balance: float,
    ) -> float:
        """Calculate optimal bet size within limits.

        Uses conservative sizing: min of max_bet and available balance.
        """
        # Can't bet more than we have on either platform
        max_by_balance = min(poly_balance, kalshi_balance)

        # Apply configured limits
        bet = min(self.max_bet, max_by_balance)
        bet = max(bet, self.min_bet)

        # Final sanity check
        if bet > max_by_balance:
            bet = max_by_balance

        return round(bet, 2)

    def record_trade(self, event_key: str, pnl: float = 0.0) -> None:
        """Record completed trade for daily tracking."""
        self._daily_trades += 1
        self._daily_pnl += pnl
        logger.info(f"Trade recorded: daily={self._daily_trades}, pnl=${self._daily_pnl:.2f}")

    def add_open_position(self, event_key: str) -> None:
        """Track open position to prevent duplicates."""
        self._open_positions.add(event_key.lower())

    def remove_open_position(self, event_key: str) -> None:
        """Remove settled position from tracking."""
        self._open_positions.discard(event_key.lower())

    def get_stats(self) -> dict:
        """Return current risk manager state."""
        return {
            "enabled": self.enabled,
            "daily_trades": self._daily_trades,
            "daily_pnl": self._daily_pnl,
            "open_positions": len(self._open_positions),
            "limits": {
                "min_bet": self.min_bet,
                "max_bet": self.max_bet,
                "min_roi": self.min_roi,
                "max_daily_trades": self.max_daily_trades,
                "max_daily_loss": self.max_daily_loss,
            },
        }
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_risk_manager.py -v`
Expected: PASS (10 tests)

**Step 5: Commit**

```bash
git add src/executor/risk_manager.py tests/test_risk_manager.py
git commit -m "feat(executor): add risk manager with pre-trade validation"
```

---

## Task 5: Add Kalshi Trading Methods

**Files:**
- Modify: `src/connectors/kalshi.py`
- Create: `tests/test_kalshi_trading.py`

**Step 1: Write the failing test**

Create: `tests/test_kalshi_trading.py`

```python
"""Tests for Kalshi trading methods."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.connectors.kalshi import KalshiConnector


@pytest.fixture
def kalshi():
    """Create Kalshi connector."""
    return KalshiConnector()


@pytest.mark.asyncio
async def test_get_balance(kalshi):
    """Should fetch account balance."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"balance": 1050}  # cents
    mock_response.raise_for_status = MagicMock()

    with patch.object(kalshi, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=mock_response)
        balance = await kalshi.get_balance()

    assert balance == 10.50  # converted to dollars


@pytest.mark.asyncio
async def test_place_order_buy_yes(kalshi):
    """Should place buy YES order."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "order": {
            "order_id": "ord_123",
            "status": "resting",
            "yes_price": 51,
            "count_total": 2,
        }
    }
    mock_response.raise_for_status = MagicMock()

    with patch.object(kalshi, "_client") as mock_client:
        mock_client.post = AsyncMock(return_value=mock_response)
        result = await kalshi.place_order(
            ticker="KXNBA-123",
            side="yes",
            action="buy",
            count=2,
            price_cents=51,
        )

    assert result["order_id"] == "ord_123"


@pytest.mark.asyncio
async def test_place_order_buy_no(kalshi):
    """Should place buy NO order."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "order": {
            "order_id": "ord_456",
            "status": "filled",
            "no_price": 48,
            "count_total": 2,
        }
    }
    mock_response.raise_for_status = MagicMock()

    with patch.object(kalshi, "_client") as mock_client:
        mock_client.post = AsyncMock(return_value=mock_response)
        result = await kalshi.place_order(
            ticker="KXNBA-123",
            side="no",
            action="buy",
            count=2,
            price_cents=48,
        )

    assert result["order_id"] == "ord_456"


@pytest.mark.asyncio
async def test_get_order_status(kalshi):
    """Should fetch order status."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "order": {
            "order_id": "ord_123",
            "status": "filled",
            "count_filled": 2,
            "count_total": 2,
        }
    }
    mock_response.raise_for_status = MagicMock()

    with patch.object(kalshi, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=mock_response)
        result = await kalshi.get_order("ord_123")

    assert result["status"] == "filled"
    assert result["count_filled"] == 2
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kalshi_trading.py -v`
Expected: FAIL with AttributeError (no get_balance, place_order methods)

**Step 3: Add trading methods to Kalshi connector**

Add to `src/connectors/kalshi.py` (at end of KalshiConnector class):

```python
    async def get_balance(self) -> float:
        """Get account balance in dollars.

        Returns:
            Balance in dollars (Kalshi API returns cents).
        """
        await self._ensure_session()
        url = f"{settings.kalshi_api_base}/portfolio/balance"
        resp = await self._client.get(url, headers=self._auth_headers("GET", "/trade-api/v2/portfolio/balance"))
        resp.raise_for_status()
        data = resp.json()
        # Kalshi returns balance in cents
        return data.get("balance", 0) / 100.0

    async def place_order(
        self,
        ticker: str,
        side: str,  # "yes" or "no"
        action: str,  # "buy" or "sell"
        count: int,
        price_cents: int,
        time_in_force: str = "fill_or_kill",
    ) -> dict:
        """Place an order on Kalshi.

        Args:
            ticker: Market ticker (e.g., "KXNBA-26FEB05-LAL-BOS")
            side: "yes" or "no"
            action: "buy" or "sell"
            count: Number of contracts
            price_cents: Price in cents (1-99)
            time_in_force: "fill_or_kill", "good_till_canceled", or "immediate_or_cancel"

        Returns:
            Order response dict with order_id, status, fill info.
        """
        await self._ensure_session()

        import uuid
        client_order_id = str(uuid.uuid4())

        payload = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
            "time_in_force": time_in_force,
            "client_order_id": client_order_id,
        }

        # Set price based on side
        if side == "yes":
            payload["yes_price"] = price_cents
        else:
            payload["no_price"] = price_cents

        url = f"{settings.kalshi_api_base}/portfolio/orders"
        resp = await self._client.post(
            url,
            json=payload,
            headers=self._auth_headers("POST", "/trade-api/v2/portfolio/orders"),
        )
        resp.raise_for_status()
        data = resp.json()

        return data.get("order", data)

    async def get_order(self, order_id: str) -> dict:
        """Get order status by ID.

        Args:
            order_id: The order ID to look up.

        Returns:
            Order dict with status, fill counts, etc.
        """
        await self._ensure_session()
        url = f"{settings.kalshi_api_base}/portfolio/orders/{order_id}"
        resp = await self._client.get(url, headers=self._auth_headers("GET", f"/trade-api/v2/portfolio/orders/{order_id}"))
        resp.raise_for_status()
        data = resp.json()
        return data.get("order", data)

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order.

        Args:
            order_id: The order ID to cancel.

        Returns:
            Cancellation response.
        """
        await self._ensure_session()
        url = f"{settings.kalshi_api_base}/portfolio/orders/{order_id}"
        resp = await self._client.delete(url, headers=self._auth_headers("DELETE", f"/trade-api/v2/portfolio/orders/{order_id}"))
        resp.raise_for_status()
        return resp.json()
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kalshi_trading.py -v`
Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add src/connectors/kalshi.py tests/test_kalshi_trading.py
git commit -m "feat(kalshi): add trading methods (get_balance, place_order, get_order)"
```

---

## Task 6: Add Polymarket Trading Methods

**Files:**
- Modify: `src/connectors/polymarket.py`
- Create: `tests/test_polymarket_trading.py`

**Step 1: Write the failing test**

Create: `tests/test_polymarket_trading.py`

```python
"""Tests for Polymarket trading methods."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.connectors.polymarket import PolymarketConnector


@pytest.fixture
def polymarket():
    """Create Polymarket connector."""
    return PolymarketConnector()


def test_trading_client_initialization(polymarket):
    """Trading client should be None until initialized."""
    assert polymarket._trading_client is None


@pytest.mark.asyncio
async def test_get_balance(polymarket):
    """Should fetch USDC balance."""
    # Mock the trading client
    mock_client = MagicMock()
    mock_client.get_balance.return_value = 10500000  # 6 decimals for USDC
    polymarket._trading_client = mock_client

    balance = await polymarket.get_balance()
    assert balance == 10.50  # converted to dollars


@pytest.mark.asyncio
async def test_place_order(polymarket):
    """Should place order via py-clob-client."""
    mock_client = MagicMock()
    mock_order = MagicMock()
    mock_client.create_and_post_order.return_value = {
        "success": True,
        "orderID": "poly_123",
    }
    polymarket._trading_client = mock_client

    result = await polymarket.place_order(
        token_id="12345",
        side="BUY",
        price=0.51,
        size=2.0,
    )

    assert result["success"]
    assert result["orderID"] == "poly_123"


@pytest.mark.asyncio
async def test_place_market_order(polymarket):
    """Should place FOK market order."""
    mock_client = MagicMock()
    mock_client.create_market_order.return_value = MagicMock()
    mock_client.post_order.return_value = {
        "success": True,
        "orderID": "poly_456",
    }
    polymarket._trading_client = mock_client

    result = await polymarket.place_market_order(
        token_id="12345",
        side="BUY",
        amount=2.0,
    )

    assert result["success"]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_polymarket_trading.py -v`
Expected: FAIL with AttributeError

**Step 3: Add trading methods to Polymarket connector**

Add to `src/connectors/polymarket.py`:

At top of file, add imports:
```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
```

In PolymarketConnector class `__init__`:
```python
        self._trading_client: ClobClient | None = None
```

Add methods at end of class:
```python
    def _ensure_trading_client(self) -> ClobClient:
        """Initialize trading client if needed."""
        if self._trading_client is None:
            from src.config import settings

            if not settings.poly_private_key:
                raise ValueError("POLY_PRIVATE_KEY not configured")

            self._trading_client = ClobClient(
                host=settings.polymarket_clob_api,
                key=settings.poly_private_key,
                chain_id=137,  # Polygon mainnet
                signature_type=1,
                funder=settings.poly_funder_address or None,
            )
            # Derive API credentials
            self._trading_client.set_api_creds(
                self._trading_client.create_or_derive_api_creds()
            )
        return self._trading_client

    async def get_balance(self) -> float:
        """Get USDC balance in dollars.

        Returns:
            Balance in dollars.
        """
        client = self._ensure_trading_client()
        # py-clob-client returns balance in USDC base units (6 decimals)
        balance_raw = client.get_balance()
        return balance_raw / 1_000_000

    async def place_order(
        self,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        price: float,
        size: float,
        order_type: str = "GTC",
    ) -> dict:
        """Place a limit order on Polymarket.

        Args:
            token_id: The conditional token ID
            side: "BUY" or "SELL"
            price: Price (0.01 to 0.99)
            size: Number of contracts
            order_type: "GTC" (good till cancelled), "FOK" (fill or kill), "GTD"

        Returns:
            Order response with success, orderID, etc.
        """
        import asyncio

        client = self._ensure_trading_client()

        order_args = OrderArgs(
            price=price,
            size=size,
            side=BUY if side.upper() == "BUY" else SELL,
            token_id=token_id,
        )

        ot = OrderType.GTC
        if order_type == "FOK":
            ot = OrderType.FOK
        elif order_type == "GTD":
            ot = OrderType.GTD

        # Run sync client method in thread pool
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: client.create_and_post_order(order_args, ot)
        )
        return result

    async def place_market_order(
        self,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        amount: float,  # Dollar amount
    ) -> dict:
        """Place a FOK market order on Polymarket.

        Args:
            token_id: The conditional token ID
            side: "BUY" or "SELL"
            amount: Dollar amount to spend/receive

        Returns:
            Order response with success, orderID, etc.
        """
        import asyncio

        client = self._ensure_trading_client()

        mo_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=BUY if side.upper() == "BUY" else SELL,
        )

        loop = asyncio.get_event_loop()
        signed = await loop.run_in_executor(
            None,
            lambda: client.create_market_order(mo_args)
        )
        result = await loop.run_in_executor(
            None,
            lambda: client.post_order(signed, OrderType.FOK)
        )
        return result
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_polymarket_trading.py -v`
Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add src/connectors/polymarket.py tests/test_polymarket_trading.py
git commit -m "feat(polymarket): add trading methods using py-clob-client"
```

---

## Task 7: Create Telegram Notifier

**Files:**
- Create: `src/executor/telegram_bot.py`
- Create: `tests/test_telegram_bot.py`

**Step 1: Write the failing test**

Create: `tests/test_telegram_bot.py`

```python
"""Tests for Telegram notifier."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, UTC

from src.executor.telegram_bot import TelegramNotifier
from src.executor.models import ExecutionResult, ExecutionStatus, LegResult, OpenPosition


@pytest.fixture
def notifier():
    """Create notifier with test credentials."""
    return TelegramNotifier(bot_token="test_token", chat_id="123456")


def test_format_execution_success(notifier):
    """Should format successful execution message."""
    poly = LegResult("polymarket", True, "p1", 1.02, 0.51, None)
    kalshi = LegResult("kalshi", True, "k1", 0.98, 0.49, None)
    result = ExecutionResult(poly_leg=poly, kalshi_leg=kalshi)

    msg = notifier._format_execution_message(
        result,
        event_title="Lakers vs Celtics",
        roi=2.3,
        profit=0.04,
    )

    assert "✅" in msg
    assert "Lakers vs Celtics" in msg
    assert "2.3%" in msg


def test_format_execution_partial(notifier):
    """Should format partial fill warning."""
    poly = LegResult("polymarket", True, "p1", 1.02, 0.51, None)
    kalshi = LegResult("kalshi", False, None, 0, 0, "Insufficient liquidity")
    result = ExecutionResult(poly_leg=poly, kalshi_leg=kalshi)

    msg = notifier._format_execution_message(
        result,
        event_title="Lakers vs Celtics",
        roi=2.3,
        profit=0.04,
    )

    assert "⚠️" in msg
    assert "PARTIAL" in msg
    assert "Insufficient liquidity" in msg


def test_format_execution_failed(notifier):
    """Should format failed execution."""
    poly = LegResult("polymarket", False, None, 0, 0, "Error 1")
    kalshi = LegResult("kalshi", False, None, 0, 0, "Error 2")
    result = ExecutionResult(poly_leg=poly, kalshi_leg=kalshi)

    msg = notifier._format_execution_message(
        result,
        event_title="Lakers vs Celtics",
        roi=2.3,
        profit=0.04,
    )

    assert "❌" in msg
    assert "FAILED" in msg


def test_format_daily_summary(notifier):
    """Should format daily summary."""
    msg = notifier._format_daily_summary(
        trades=15,
        successful=14,
        partial=1,
        pnl=0.87,
        poly_balance=10.87,
        kalshi_balance=10.43,
    )

    assert "📊" in msg
    assert "15" in msg
    assert "93%" in msg  # 14/15
    assert "$0.87" in msg


@pytest.mark.asyncio
async def test_send_message(notifier):
    """Should send message via Telegram API."""
    with patch("src.executor.telegram_bot.Bot") as MockBot:
        mock_bot = AsyncMock()
        MockBot.return_value = mock_bot

        await notifier.send("Test message")

        mock_bot.send_message.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telegram_bot.py -v`
Expected: FAIL with ModuleNotFoundError

**Step 3: Implement Telegram notifier**

Create `src/executor/telegram_bot.py`:

```python
"""Telegram bot for notifications and commands."""

from __future__ import annotations

import logging
from datetime import datetime, UTC

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from src.executor.models import ExecutionResult, ExecutionStatus

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends notifications and handles commands via Telegram."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._bot: Bot | None = None
        self._app: Application | None = None

    def _get_bot(self) -> Bot:
        """Get or create bot instance."""
        if self._bot is None:
            self._bot = Bot(token=self.bot_token)
        return self._bot

    async def send(self, message: str) -> None:
        """Send a message to configured chat."""
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram not configured, skipping notification")
            return

        try:
            bot = self._get_bot()
            await bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

    async def notify_execution(
        self,
        result: ExecutionResult,
        event_title: str,
        roi: float,
        profit: float,
        poly_balance: float = 0,
        kalshi_balance: float = 0,
    ) -> None:
        """Send execution result notification."""
        msg = self._format_execution_message(
            result, event_title, roi, profit, poly_balance, kalshi_balance
        )
        await self.send(msg)

    async def notify_kill_switch(self, reason: str, daily_trades: int, pnl: float) -> None:
        """Send kill switch activation notification."""
        msg = (
            "🛑 <b>KILL SWITCH ACTIVATED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Reason: {reason}\n"
            f"Trades today: {daily_trades}\n"
            f"P&L: ${pnl:+.2f}\n\n"
            "Send /start to resume"
        )
        await self.send(msg)

    async def notify_daily_summary(
        self,
        trades: int,
        successful: int,
        partial: int,
        pnl: float,
        poly_balance: float,
        kalshi_balance: float,
    ) -> None:
        """Send end-of-day summary."""
        msg = self._format_daily_summary(
            trades, successful, partial, pnl, poly_balance, kalshi_balance
        )
        await self.send(msg)

    def _format_execution_message(
        self,
        result: ExecutionResult,
        event_title: str,
        roi: float,
        profit: float,
        poly_balance: float = 0,
        kalshi_balance: float = 0,
    ) -> str:
        """Format execution result as Telegram message."""
        if result.status == ExecutionStatus.SUCCESS:
            header = "✅ <b>EXECUTED</b> — Successful arbitrage"
            status_details = ""
        elif result.status == ExecutionStatus.PARTIAL:
            header = "⚠️ <b>PARTIAL FILL</b> — Attention required!"
            failed_leg = result.kalshi_leg if not result.kalshi_leg.success else result.poly_leg
            status_details = f"\n🔴 {failed_leg.platform}: {failed_leg.error}"
        else:
            header = "❌ <b>FAILED</b> — Both legs failed"
            status_details = (
                f"\nPoly: {result.poly_leg.error}"
                f"\nKalshi: {result.kalshi_leg.error}"
            )

        msg = (
            f"{header}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏀 {event_title}\n"
            f"📈 ROI: {roi:.1f}% (${profit:.2f} profit)\n"
        )

        if result.poly_leg.success:
            msg += f"\nPoly: ✅ ${result.poly_leg.filled_amount:.2f} @ {result.poly_leg.filled_price:.2f}"
        else:
            msg += f"\nPoly: ❌ {result.poly_leg.error or 'Failed'}"

        if result.kalshi_leg.success:
            msg += f"\nKalshi: ✅ ${result.kalshi_leg.filled_amount:.2f} @ {result.kalshi_leg.filled_price:.2f}"
        else:
            msg += f"\nKalshi: ❌ {result.kalshi_leg.error or 'Failed'}"

        if status_details:
            msg += status_details

        if poly_balance > 0 or kalshi_balance > 0:
            msg += f"\n\n💰 Balances: Poly ${poly_balance:.2f} | Kalshi ${kalshi_balance:.2f}"

        return msg

    def _format_daily_summary(
        self,
        trades: int,
        successful: int,
        partial: int,
        pnl: float,
        poly_balance: float,
        kalshi_balance: float,
    ) -> str:
        """Format daily summary message."""
        success_rate = (successful / trades * 100) if trades > 0 else 0

        return (
            "📊 <b>DAILY SUMMARY</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Trades: {trades}\n"
            f"Successful: {successful} ({success_rate:.0f}%)\n"
            f"Partial: {partial}\n\n"
            f"P&L: ${pnl:+.2f}\n"
            f"Balances: Poly ${poly_balance:.2f} | Kalshi ${kalshi_balance:.2f}"
        )
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_telegram_bot.py -v`
Expected: PASS (6 tests)

**Step 5: Commit**

```bash
git add src/executor/telegram_bot.py tests/test_telegram_bot.py
git commit -m "feat(executor): add Telegram notifier for trade alerts"
```

---

## Task 8: Create Order Placer

**Files:**
- Create: `src/executor/order_placer.py`
- Create: `tests/test_order_placer.py`

**Step 1: Write the failing test**

Create: `tests/test_order_placer.py`

```python
"""Tests for order placer."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, UTC

from src.executor.order_placer import OrderPlacer
from src.executor.models import ExecutionStatus
from src.models import ArbitrageOpportunity, Platform


@pytest.fixture
def sample_opportunity():
    """Create sample arbitrage opportunity."""
    return ArbitrageOpportunity(
        event_title="Lakers vs Celtics",
        team_a="Lakers",
        team_b="Celtics",
        platform_buy_yes=Platform.POLYMARKET,
        platform_buy_no=Platform.KALSHI,
        yes_price=0.51,
        no_price=0.48,
        total_cost=0.99,
        profit_pct=1.0,
        roi_after_fees=2.5,
        found_at=datetime.now(UTC),
        details={
            "poly_token_id": "token_123",
            "kalshi_ticker": "KXNBA-123",
            "poly_url": "https://polymarket.com/...",
            "kalshi_url": "https://kalshi.com/...",
        },
    )


@pytest.mark.asyncio
async def test_execute_both_success(sample_opportunity):
    """Should return SUCCESS when both legs fill."""
    mock_poly = AsyncMock()
    mock_poly.place_order.return_value = {
        "success": True,
        "orderID": "poly_123",
    }

    mock_kalshi = AsyncMock()
    mock_kalshi.place_order.return_value = {
        "order_id": "kalshi_456",
        "status": "filled",
        "count_filled": 2,
    }

    placer = OrderPlacer(poly_connector=mock_poly, kalshi_connector=mock_kalshi)
    result = await placer.execute(sample_opportunity, bet_size=2.0)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.poly_leg.success
    assert result.kalshi_leg.success


@pytest.mark.asyncio
async def test_execute_poly_fails(sample_opportunity):
    """Should return PARTIAL when Poly fails."""
    mock_poly = AsyncMock()
    mock_poly.place_order.side_effect = Exception("Insufficient balance")

    mock_kalshi = AsyncMock()
    mock_kalshi.place_order.return_value = {
        "order_id": "kalshi_456",
        "status": "filled",
    }

    placer = OrderPlacer(poly_connector=mock_poly, kalshi_connector=mock_kalshi)
    result = await placer.execute(sample_opportunity, bet_size=2.0)

    assert result.status == ExecutionStatus.PARTIAL
    assert not result.poly_leg.success
    assert result.kalshi_leg.success


@pytest.mark.asyncio
async def test_execute_kalshi_fails(sample_opportunity):
    """Should return PARTIAL when Kalshi fails."""
    mock_poly = AsyncMock()
    mock_poly.place_order.return_value = {
        "success": True,
        "orderID": "poly_123",
    }

    mock_kalshi = AsyncMock()
    mock_kalshi.place_order.side_effect = Exception("Market closed")

    placer = OrderPlacer(poly_connector=mock_poly, kalshi_connector=mock_kalshi)
    result = await placer.execute(sample_opportunity, bet_size=2.0)

    assert result.status == ExecutionStatus.PARTIAL
    assert result.poly_leg.success
    assert not result.kalshi_leg.success


@pytest.mark.asyncio
async def test_execute_both_fail(sample_opportunity):
    """Should return FAILED when both legs fail."""
    mock_poly = AsyncMock()
    mock_poly.place_order.side_effect = Exception("Poly error")

    mock_kalshi = AsyncMock()
    mock_kalshi.place_order.side_effect = Exception("Kalshi error")

    placer = OrderPlacer(poly_connector=mock_poly, kalshi_connector=mock_kalshi)
    result = await placer.execute(sample_opportunity, bet_size=2.0)

    assert result.status == ExecutionStatus.FAILED
    assert not result.poly_leg.success
    assert not result.kalshi_leg.success


@pytest.mark.asyncio
async def test_calculate_leg_sizes():
    """Should calculate proportional leg sizes."""
    placer = OrderPlacer(poly_connector=MagicMock(), kalshi_connector=MagicMock())

    poly_size, kalshi_size = placer._calculate_leg_sizes(
        bet_size=2.0,
        poly_price=0.51,
        kalshi_price=0.48,
    )

    # Total should equal bet_size
    assert abs((poly_size + kalshi_size) - 2.0) < 0.01
    # Sizes should be proportional to prices
    assert poly_size > kalshi_size  # Higher price = more dollars allocated
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_order_placer.py -v`
Expected: FAIL with ModuleNotFoundError

**Step 3: Implement order placer**

Create `src/executor/order_placer.py`:

```python
"""Order placer for executing arbitrage trades."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, UTC

from src.executor.models import ExecutionResult, LegResult
from src.models import ArbitrageOpportunity, Platform

logger = logging.getLogger(__name__)


class OrderPlacer:
    """Executes arbitrage by placing orders on both platforms."""

    def __init__(self, poly_connector, kalshi_connector):
        self.poly = poly_connector
        self.kalshi = kalshi_connector

    async def execute(
        self,
        opp: ArbitrageOpportunity,
        bet_size: float,
    ) -> ExecutionResult:
        """Execute arbitrage by placing both legs in parallel.

        Args:
            opp: The arbitrage opportunity to execute
            bet_size: Total dollar amount to bet

        Returns:
            ExecutionResult with status and leg details
        """
        # Calculate sizes for each leg
        poly_size, kalshi_size = self._calculate_leg_sizes(
            bet_size,
            opp.yes_price,
            opp.no_price,
        )

        # Determine sides based on which platform is buy YES vs buy NO
        if opp.platform_buy_yes == Platform.POLYMARKET:
            poly_side = "BUY"
            kalshi_side = "no"
            kalshi_action = "buy"
        else:
            poly_side = "SELL"  # Buying NO = selling YES on Poly
            kalshi_side = "yes"
            kalshi_action = "buy"

        # Get market identifiers from details
        poly_token_id = opp.details.get("poly_token_id", "")
        kalshi_ticker = opp.details.get("kalshi_ticker", "")

        if not poly_token_id or not kalshi_ticker:
            logger.error(f"Missing market IDs: poly={poly_token_id}, kalshi={kalshi_ticker}")
            return ExecutionResult(
                poly_leg=LegResult("polymarket", False, None, 0, 0, "Missing token_id"),
                kalshi_leg=LegResult("kalshi", False, None, 0, 0, "Missing ticker"),
            )

        # Execute both legs in parallel
        poly_task = self._execute_poly_leg(
            poly_token_id, poly_side, opp.yes_price, poly_size
        )
        kalshi_task = self._execute_kalshi_leg(
            kalshi_ticker, kalshi_side, kalshi_action, opp.no_price, kalshi_size
        )

        poly_result, kalshi_result = await asyncio.gather(
            poly_task, kalshi_task,
            return_exceptions=True,
        )

        # Convert exceptions to LegResults
        if isinstance(poly_result, Exception):
            poly_result = LegResult("polymarket", False, None, 0, 0, str(poly_result))
        if isinstance(kalshi_result, Exception):
            kalshi_result = LegResult("kalshi", False, None, 0, 0, str(kalshi_result))

        return ExecutionResult(poly_leg=poly_result, kalshi_leg=kalshi_result)

    async def _execute_poly_leg(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> LegResult:
        """Execute Polymarket leg."""
        try:
            # Calculate number of contracts from dollar size and price
            contracts = size / price if price > 0 else 0

            result = await self.poly.place_order(
                token_id=token_id,
                side=side,
                price=price,
                size=contracts,
                order_type="FOK",  # Fill or kill for immediate execution
            )

            success = result.get("success", False)
            order_id = result.get("orderID") or result.get("order_id")

            return LegResult(
                platform="polymarket",
                success=success,
                order_id=order_id,
                filled_amount=size if success else 0,
                filled_price=price if success else 0,
                error=result.get("errorMsg") if not success else None,
            )
        except Exception as e:
            logger.error(f"Polymarket order failed: {e}")
            return LegResult("polymarket", False, None, 0, 0, str(e))

    async def _execute_kalshi_leg(
        self,
        ticker: str,
        side: str,
        action: str,
        price: float,
        size: float,
    ) -> LegResult:
        """Execute Kalshi leg."""
        try:
            # Calculate number of contracts (Kalshi uses integer contracts)
            # Each contract pays $1, so contracts = size / (1 - price) for NO side
            if side == "no":
                contracts = int(size / (1 - price)) if price < 1 else 0
                price_cents = int((1 - price) * 100)  # NO price
            else:
                contracts = int(size / price) if price > 0 else 0
                price_cents = int(price * 100)

            contracts = max(1, contracts)  # At least 1 contract

            result = await self.kalshi.place_order(
                ticker=ticker,
                side=side,
                action=action,
                count=contracts,
                price_cents=price_cents,
                time_in_force="fill_or_kill",
            )

            order_id = result.get("order_id")
            status = result.get("status", "")
            success = status in ("filled", "resting") or order_id is not None

            return LegResult(
                platform="kalshi",
                success=success,
                order_id=order_id,
                filled_amount=size if success else 0,
                filled_price=price if success else 0,
                error=result.get("error") if not success else None,
            )
        except Exception as e:
            logger.error(f"Kalshi order failed: {e}")
            return LegResult("kalshi", False, None, 0, 0, str(e))

    def _calculate_leg_sizes(
        self,
        bet_size: float,
        poly_price: float,
        kalshi_price: float,
    ) -> tuple[float, float]:
        """Calculate dollar amounts for each leg.

        Allocates proportionally to prices so that payout is equal
        regardless of which outcome wins.
        """
        total_price = poly_price + kalshi_price
        if total_price <= 0:
            return bet_size / 2, bet_size / 2

        poly_ratio = poly_price / total_price
        kalshi_ratio = kalshi_price / total_price

        poly_size = round(bet_size * poly_ratio, 2)
        kalshi_size = round(bet_size * kalshi_ratio, 2)

        # Ensure total matches bet_size
        diff = bet_size - (poly_size + kalshi_size)
        if diff != 0:
            poly_size = round(poly_size + diff, 2)

        return poly_size, kalshi_size
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_order_placer.py -v`
Expected: PASS (5 tests)

**Step 5: Commit**

```bash
git add src/executor/order_placer.py tests/test_order_placer.py
git commit -m "feat(executor): add order placer with parallel leg execution"
```

---

## Task 9: Create Position Manager

**Files:**
- Create: `src/executor/position_manager.py`
- Modify: `src/db.py` (add positions table)

**Step 1: Write the failing test**

Create: `tests/test_position_manager.py`

```python
"""Tests for position manager."""

import pytest
from datetime import datetime, UTC

from src.executor.position_manager import PositionManager
from src.executor.models import OpenPosition


@pytest.fixture
async def position_manager(tmp_path):
    """Create position manager with temp database."""
    db_path = str(tmp_path / "test.db")
    pm = PositionManager(db_path)
    await pm.connect()
    yield pm
    await pm.close()


@pytest.fixture
def sample_position():
    """Create sample open position."""
    return OpenPosition(
        id="pos_123",
        event_title="Lakers vs Celtics",
        team_a="Lakers",
        team_b="Celtics",
        poly_side="YES",
        poly_amount=1.02,
        poly_contracts=2.0,
        poly_avg_price=0.51,
        poly_order_id="poly_abc",
        kalshi_side="no",
        kalshi_amount=0.98,
        kalshi_contracts=2,
        kalshi_avg_price=0.49,
        kalshi_order_id="kalshi_xyz",
        arb_type="yes_no",
        expected_roi=2.1,
        opened_at=datetime.now(UTC),
        status="open",
    )


@pytest.mark.asyncio
async def test_save_position(position_manager, sample_position):
    """Should save position to database."""
    await position_manager.save_position(sample_position)

    positions = await position_manager.get_open_positions()
    assert len(positions) == 1
    assert positions[0].id == "pos_123"


@pytest.mark.asyncio
async def test_get_open_positions(position_manager, sample_position):
    """Should return only open positions."""
    await position_manager.save_position(sample_position)

    # Create and save a settled position
    settled = OpenPosition(
        id="pos_456",
        event_title="Heat vs Bulls",
        team_a="Heat",
        team_b="Bulls",
        poly_side="YES",
        poly_amount=1.0,
        poly_contracts=2.0,
        poly_avg_price=0.50,
        kalshi_side="no",
        kalshi_amount=1.0,
        kalshi_contracts=2,
        kalshi_avg_price=0.50,
        arb_type="yes_no",
        expected_roi=2.0,
        status="settled",
    )
    await position_manager.save_position(settled)

    positions = await position_manager.get_open_positions()
    assert len(positions) == 1
    assert positions[0].id == "pos_123"


@pytest.mark.asyncio
async def test_settle_position(position_manager, sample_position):
    """Should mark position as settled with P&L."""
    await position_manager.save_position(sample_position)

    await position_manager.settle_position(
        position_id="pos_123",
        actual_pnl=0.05,
        winning_side="poly",
    )

    positions = await position_manager.get_open_positions()
    assert len(positions) == 0

    pos = await position_manager.get_position("pos_123")
    assert pos.status == "settled"
    assert pos.actual_pnl == 0.05


@pytest.mark.asyncio
async def test_get_daily_stats(position_manager, sample_position):
    """Should calculate daily statistics."""
    await position_manager.save_position(sample_position)
    await position_manager.settle_position("pos_123", 0.05, "poly")

    stats = await position_manager.get_daily_stats()
    assert stats["trades"] == 1
    assert stats["pnl"] == 0.05
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_position_manager.py -v`
Expected: FAIL with ModuleNotFoundError

**Step 3: Add positions schema to db.py**

Add to `src/db.py` SCHEMA constant:

```python
# Add after existing schema
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    event_title TEXT NOT NULL,
    team_a TEXT NOT NULL,
    team_b TEXT NOT NULL,
    poly_side TEXT NOT NULL,
    poly_amount REAL NOT NULL,
    poly_contracts REAL NOT NULL,
    poly_avg_price REAL NOT NULL,
    poly_order_id TEXT DEFAULT '',
    kalshi_side TEXT NOT NULL,
    kalshi_amount REAL NOT NULL,
    kalshi_contracts INTEGER NOT NULL,
    kalshi_avg_price REAL NOT NULL,
    kalshi_order_id TEXT DEFAULT '',
    arb_type TEXT NOT NULL,
    expected_roi REAL NOT NULL,
    opened_at TEXT NOT NULL,
    status TEXT DEFAULT 'open',
    settled_at TEXT,
    actual_pnl REAL,
    winning_side TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status, opened_at);
```

**Step 4: Implement position manager**

Create `src/executor/position_manager.py`:

```python
"""Position manager for tracking open arbitrage positions."""

from __future__ import annotations

import json
import logging
from datetime import datetime, UTC, date

import aiosqlite

from src.executor.models import OpenPosition

logger = logging.getLogger(__name__)

_POSITIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    event_title TEXT NOT NULL,
    team_a TEXT NOT NULL,
    team_b TEXT NOT NULL,
    poly_side TEXT NOT NULL,
    poly_amount REAL NOT NULL,
    poly_contracts REAL NOT NULL,
    poly_avg_price REAL NOT NULL,
    poly_order_id TEXT DEFAULT '',
    kalshi_side TEXT NOT NULL,
    kalshi_amount REAL NOT NULL,
    kalshi_contracts INTEGER NOT NULL,
    kalshi_avg_price REAL NOT NULL,
    kalshi_order_id TEXT DEFAULT '',
    arb_type TEXT NOT NULL,
    expected_roi REAL NOT NULL,
    opened_at TEXT NOT NULL,
    status TEXT DEFAULT 'open',
    settled_at TEXT,
    actual_pnl REAL,
    winning_side TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status, opened_at);
"""


class PositionManager:
    """Manages open positions and settlement tracking."""

    def __init__(self, db_path: str = ""):
        from src.config import settings
        self.db_path = db_path or settings.db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Initialize database connection."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_POSITIONS_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def save_position(self, pos: OpenPosition) -> None:
        """Save new position to database."""
        await self._db.execute(
            """
            INSERT OR REPLACE INTO positions (
                id, event_title, team_a, team_b,
                poly_side, poly_amount, poly_contracts, poly_avg_price, poly_order_id,
                kalshi_side, kalshi_amount, kalshi_contracts, kalshi_avg_price, kalshi_order_id,
                arb_type, expected_roi, opened_at, status, settled_at, actual_pnl, winning_side
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pos.id, pos.event_title, pos.team_a, pos.team_b,
                pos.poly_side, pos.poly_amount, pos.poly_contracts, pos.poly_avg_price, pos.poly_order_id,
                pos.kalshi_side, pos.kalshi_amount, pos.kalshi_contracts, pos.kalshi_avg_price, pos.kalshi_order_id,
                pos.arb_type, pos.expected_roi,
                pos.opened_at.isoformat() if pos.opened_at else datetime.now(UTC).isoformat(),
                pos.status,
                pos.settled_at.isoformat() if pos.settled_at else None,
                pos.actual_pnl,
                pos.winning_side,
            ),
        )
        await self._db.commit()

    async def get_position(self, position_id: str) -> OpenPosition | None:
        """Get position by ID."""
        cursor = await self._db.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_position(row)

    async def get_open_positions(self) -> list[OpenPosition]:
        """Get all open (unsettled) positions."""
        cursor = await self._db.execute(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY opened_at"
        )
        rows = await cursor.fetchall()
        return [self._row_to_position(row) for row in rows]

    async def settle_position(
        self,
        position_id: str,
        actual_pnl: float,
        winning_side: str,
    ) -> None:
        """Mark position as settled with actual P&L."""
        await self._db.execute(
            """
            UPDATE positions
            SET status = 'settled',
                settled_at = ?,
                actual_pnl = ?,
                winning_side = ?
            WHERE id = ?
            """,
            (datetime.now(UTC).isoformat(), actual_pnl, winning_side, position_id),
        )
        await self._db.commit()

    async def get_daily_stats(self, day: date | None = None) -> dict:
        """Get statistics for a specific day."""
        if day is None:
            day = date.today()

        day_start = f"{day.isoformat()}T00:00:00"
        day_end = f"{day.isoformat()}T23:59:59"

        # Count trades
        cursor = await self._db.execute(
            """
            SELECT COUNT(*) as trades,
                   SUM(CASE WHEN status = 'settled' THEN 1 ELSE 0 END) as settled,
                   SUM(CASE WHEN status = 'partial' THEN 1 ELSE 0 END) as partial,
                   COALESCE(SUM(actual_pnl), 0) as pnl
            FROM positions
            WHERE opened_at >= ? AND opened_at <= ?
            """,
            (day_start, day_end),
        )
        row = await cursor.fetchone()

        return {
            "trades": row["trades"] or 0,
            "settled": row["settled"] or 0,
            "partial": row["partial"] or 0,
            "pnl": row["pnl"] or 0.0,
        }

    def _row_to_position(self, row: aiosqlite.Row) -> OpenPosition:
        """Convert database row to OpenPosition."""
        return OpenPosition(
            id=row["id"],
            event_title=row["event_title"],
            team_a=row["team_a"],
            team_b=row["team_b"],
            poly_side=row["poly_side"],
            poly_amount=row["poly_amount"],
            poly_contracts=row["poly_contracts"],
            poly_avg_price=row["poly_avg_price"],
            poly_order_id=row["poly_order_id"] or "",
            kalshi_side=row["kalshi_side"],
            kalshi_amount=row["kalshi_amount"],
            kalshi_contracts=row["kalshi_contracts"],
            kalshi_avg_price=row["kalshi_avg_price"],
            kalshi_order_id=row["kalshi_order_id"] or "",
            arb_type=row["arb_type"],
            expected_roi=row["expected_roi"],
            opened_at=datetime.fromisoformat(row["opened_at"]) if row["opened_at"] else datetime.now(UTC),
            status=row["status"],
            settled_at=datetime.fromisoformat(row["settled_at"]) if row["settled_at"] else None,
            actual_pnl=row["actual_pnl"],
            winning_side=row["winning_side"],
        )
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_position_manager.py -v`
Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add src/executor/position_manager.py src/db.py tests/test_position_manager.py
git commit -m "feat(executor): add position manager for tracking open positions"
```

---

## Task 10: Create Main Executor

**Files:**
- Create: `src/executor/executor.py`
- Modify: `src/executor/__init__.py`

**Step 1: Write the failing test**

Create: `tests/test_executor.py`

```python
"""Tests for main executor."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, UTC

from src.executor.executor import Executor
from src.executor.models import ExecutionStatus, LegResult, ExecutionResult
from src.models import ArbitrageOpportunity, Platform


@pytest.fixture
def mock_components():
    """Create mock executor components."""
    return {
        "risk_manager": MagicMock(),
        "order_placer": AsyncMock(),
        "position_manager": AsyncMock(),
        "telegram": AsyncMock(),
        "poly_connector": AsyncMock(),
        "kalshi_connector": AsyncMock(),
    }


@pytest.fixture
def sample_opportunity():
    """Create sample opportunity."""
    return ArbitrageOpportunity(
        event_title="Lakers vs Celtics",
        team_a="Lakers",
        team_b="Celtics",
        platform_buy_yes=Platform.POLYMARKET,
        platform_buy_no=Platform.KALSHI,
        yes_price=0.51,
        no_price=0.48,
        total_cost=0.99,
        profit_pct=1.0,
        roi_after_fees=2.5,
        found_at=datetime.now(UTC),
        details={
            "poly_token_id": "token_123",
            "kalshi_ticker": "KXNBA-123",
        },
    )


@pytest.mark.asyncio
async def test_executor_skips_when_disabled(mock_components, sample_opportunity):
    """Should skip execution when disabled."""
    mock_components["risk_manager"].enabled = False
    mock_components["risk_manager"].check_opportunity.return_value = MagicMock(
        passed=False, reason="Kill switch is OFF"
    )

    executor = Executor(**mock_components)
    result = await executor.try_execute(sample_opportunity)

    assert result is None
    mock_components["order_placer"].execute.assert_not_called()


@pytest.mark.asyncio
async def test_executor_skips_failed_risk_check(mock_components, sample_opportunity):
    """Should skip execution when risk check fails."""
    mock_components["risk_manager"].enabled = True
    mock_components["risk_manager"].check_opportunity.return_value = MagicMock(
        passed=False, reason="ROI too low"
    )
    mock_components["poly_connector"].get_balance.return_value = 10.0
    mock_components["kalshi_connector"].get_balance.return_value = 10.0

    executor = Executor(**mock_components)
    result = await executor.try_execute(sample_opportunity)

    assert result is None


@pytest.mark.asyncio
async def test_executor_executes_valid_opportunity(mock_components, sample_opportunity):
    """Should execute valid opportunity."""
    mock_components["risk_manager"].enabled = True
    mock_components["risk_manager"].check_opportunity.return_value = MagicMock(passed=True)
    mock_components["risk_manager"].calculate_bet_size.return_value = 2.0
    mock_components["poly_connector"].get_balance.return_value = 10.0
    mock_components["kalshi_connector"].get_balance.return_value = 10.0

    poly_leg = LegResult("polymarket", True, "p1", 1.02, 0.51, None)
    kalshi_leg = LegResult("kalshi", True, "k1", 0.98, 0.49, None)
    mock_components["order_placer"].execute.return_value = ExecutionResult(
        poly_leg=poly_leg, kalshi_leg=kalshi_leg
    )

    executor = Executor(**mock_components)
    result = await executor.try_execute(sample_opportunity)

    assert result is not None
    assert result.status == ExecutionStatus.SUCCESS
    mock_components["telegram"].notify_execution.assert_called_once()
    mock_components["position_manager"].save_position.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_executor.py -v`
Expected: FAIL with ModuleNotFoundError

**Step 3: Implement main executor**

Create `src/executor/executor.py`:

```python
"""Main executor that orchestrates arbitrage execution."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, UTC

from src.executor.models import ExecutionResult, ExecutionStatus, OpenPosition
from src.executor.order_placer import OrderPlacer
from src.executor.position_manager import PositionManager
from src.executor.risk_manager import RiskManager
from src.executor.telegram_bot import TelegramNotifier
from src.models import ArbitrageOpportunity

logger = logging.getLogger(__name__)


class Executor:
    """Orchestrates arbitrage detection and execution."""

    def __init__(
        self,
        risk_manager: RiskManager,
        order_placer: OrderPlacer,
        position_manager: PositionManager,
        telegram: TelegramNotifier,
        poly_connector,
        kalshi_connector,
    ):
        self.risk = risk_manager
        self.placer = order_placer
        self.positions = position_manager
        self.telegram = telegram
        self.poly = poly_connector
        self.kalshi = kalshi_connector

    async def try_execute(self, opp: ArbitrageOpportunity) -> ExecutionResult | None:
        """Attempt to execute an arbitrage opportunity.

        Returns ExecutionResult if executed, None if skipped.
        """
        # Get current balances
        try:
            poly_balance = await self.poly.get_balance()
            kalshi_balance = await self.kalshi.get_balance()
        except Exception as e:
            logger.error(f"Failed to fetch balances: {e}")
            return None

        # Run risk checks
        check = self.risk.check_opportunity(opp, poly_balance, kalshi_balance)
        if not check.passed:
            logger.debug(f"Risk check failed for {opp.event_title}: {check.reason}")
            return None

        # Calculate bet size
        bet_size = self.risk.calculate_bet_size(opp, poly_balance, kalshi_balance)
        if bet_size <= 0:
            logger.debug(f"Bet size too small for {opp.event_title}")
            return None

        logger.info(f"EXECUTING: {opp.event_title} | ROI={opp.roi_after_fees}% | bet=${bet_size}")

        # Execute the trade
        result = await self.placer.execute(opp, bet_size)

        # Calculate expected profit
        expected_profit = bet_size * (opp.roi_after_fees / 100)

        # Handle result
        if result.status == ExecutionStatus.SUCCESS:
            # Save position
            position = self._create_position(opp, result, bet_size)
            await self.positions.save_position(position)
            self.risk.add_open_position(f"{opp.team_a}:{opp.team_b}")
            self.risk.record_trade(f"{opp.team_a}:{opp.team_b}")

            logger.info(f"SUCCESS: {opp.event_title} | Expected profit: ${expected_profit:.2f}")

        elif result.status == ExecutionStatus.PARTIAL:
            # Partial fill - save position with partial status
            position = self._create_position(opp, result, bet_size)
            position.status = "partial"
            await self.positions.save_position(position)
            self.risk.add_open_position(f"{opp.team_a}:{opp.team_b}")

            logger.warning(f"PARTIAL: {opp.event_title} - needs attention!")

        else:
            logger.warning(f"FAILED: {opp.event_title} - both legs failed")

        # Send notification
        try:
            new_poly_balance = await self.poly.get_balance()
            new_kalshi_balance = await self.kalshi.get_balance()
        except Exception:
            new_poly_balance = poly_balance
            new_kalshi_balance = kalshi_balance

        await self.telegram.notify_execution(
            result=result,
            event_title=opp.event_title,
            roi=opp.roi_after_fees,
            profit=expected_profit,
            poly_balance=new_poly_balance,
            kalshi_balance=new_kalshi_balance,
        )

        # Check if we hit kill switch conditions
        stats = self.risk.get_stats()
        if stats["daily_pnl"] <= -self.risk.max_daily_loss:
            self.risk.enabled = False
            await self.telegram.notify_kill_switch(
                reason="Daily loss limit reached",
                daily_trades=stats["daily_trades"],
                pnl=stats["daily_pnl"],
            )

        return result

    def _create_position(
        self,
        opp: ArbitrageOpportunity,
        result: ExecutionResult,
        bet_size: float,
    ) -> OpenPosition:
        """Create position record from execution result."""
        return OpenPosition(
            id=str(uuid.uuid4()),
            event_title=opp.event_title,
            team_a=opp.team_a,
            team_b=opp.team_b,
            poly_side="YES" if opp.platform_buy_yes.value == "polymarket" else "NO",
            poly_amount=result.poly_leg.filled_amount,
            poly_contracts=result.poly_leg.filled_amount / opp.yes_price if opp.yes_price > 0 else 0,
            poly_avg_price=result.poly_leg.filled_price,
            poly_order_id=result.poly_leg.order_id or "",
            kalshi_side="no" if opp.platform_buy_no.value == "kalshi" else "yes",
            kalshi_amount=result.kalshi_leg.filled_amount,
            kalshi_contracts=int(result.kalshi_leg.filled_amount / opp.no_price) if opp.no_price > 0 else 0,
            kalshi_avg_price=result.kalshi_leg.filled_price,
            kalshi_order_id=result.kalshi_leg.order_id or "",
            arb_type=opp.details.get("arb_type", "yes_no"),
            expected_roi=opp.roi_after_fees,
            opened_at=datetime.now(UTC),
            status="open",
        )

    async def check_settlements(self) -> None:
        """Check open positions for settlement."""
        positions = await self.positions.get_open_positions()

        for pos in positions:
            # TODO: Check if event has settled on both platforms
            # For now, this is a placeholder for manual settlement
            logger.debug(f"Checking settlement for: {pos.event_title}")

    async def send_daily_summary(self) -> None:
        """Send end-of-day summary via Telegram."""
        stats = await self.positions.get_daily_stats()

        try:
            poly_balance = await self.poly.get_balance()
            kalshi_balance = await self.kalshi.get_balance()
        except Exception:
            poly_balance = 0
            kalshi_balance = 0

        await self.telegram.notify_daily_summary(
            trades=stats["trades"],
            successful=stats["settled"],
            partial=stats["partial"],
            pnl=stats["pnl"],
            poly_balance=poly_balance,
            kalshi_balance=kalshi_balance,
        )
```

Update `src/executor/__init__.py`:

```python
"""Executor module for automatic arbitrage execution."""

from src.executor.executor import Executor
from src.executor.models import (
    ExecutionResult,
    ExecutionStatus,
    LegResult,
    OpenPosition,
    RiskCheckResult,
)
from src.executor.order_placer import OrderPlacer
from src.executor.position_manager import PositionManager
from src.executor.risk_manager import RiskManager
from src.executor.telegram_bot import TelegramNotifier

__all__ = [
    "Executor",
    "ExecutionResult",
    "ExecutionStatus",
    "LegResult",
    "OpenPosition",
    "OrderPlacer",
    "PositionManager",
    "RiskCheckResult",
    "RiskManager",
    "TelegramNotifier",
]
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_executor.py -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add src/executor/ tests/test_executor.py
git commit -m "feat(executor): add main executor orchestration"
```

---

## Task 11: Integrate Executor with Main Loop

**Files:**
- Modify: `src/main.py`

**Step 1: Write integration test**

Create: `tests/test_main_executor_integration.py`

```python
"""Integration tests for executor in main loop."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def test_executor_initialization_when_disabled():
    """Executor should not initialize when EXECUTOR_ENABLED=False."""
    with patch("src.config.settings") as mock_settings:
        mock_settings.executor_enabled = False

        # Import after patching
        from src.main import _create_executor

        executor = _create_executor()
        assert executor is None


def test_executor_initialization_when_enabled():
    """Executor should initialize when EXECUTOR_ENABLED=True."""
    with patch("src.config.settings") as mock_settings:
        mock_settings.executor_enabled = True
        mock_settings.telegram_bot_token = "test_token"
        mock_settings.telegram_chat_id = "123456"
        mock_settings.executor_min_bet = 1.0
        mock_settings.executor_max_bet = 2.0
        mock_settings.executor_min_roi = 1.0
        mock_settings.executor_max_daily_trades = 50
        mock_settings.executor_max_daily_loss = 5.0
        mock_settings.executor_min_platform_balance = 1.0
        mock_settings.poly_private_key = ""
        mock_settings.poly_funder_address = ""

        from src.main import _create_executor

        executor = _create_executor()
        # Will be None if poly_private_key is empty (can't trade without it)
        # This is expected behavior
```

**Step 2: Run test to verify current state**

Run: `uv run pytest tests/test_main_executor_integration.py -v`

**Step 3: Add executor integration to main.py**

Add to `src/main.py` imports:

```python
from src.executor import (
    Executor,
    OrderPlacer,
    PositionManager,
    RiskManager,
    TelegramNotifier,
)
```

Add helper function:

```python
def _create_executor() -> Executor | None:
    """Create executor if enabled and configured."""
    if not settings.executor_enabled:
        logger.info("Executor disabled (EXECUTOR_ENABLED=false)")
        return None

    if not settings.poly_private_key:
        logger.warning("Executor disabled: POLY_PRIVATE_KEY not configured")
        return None

    if not settings.telegram_bot_token:
        logger.warning("Executor running without Telegram notifications")

    # Create components
    risk_manager = RiskManager(
        min_bet=settings.executor_min_bet,
        max_bet=settings.executor_max_bet,
        min_roi=settings.executor_min_roi,
        max_daily_trades=settings.executor_max_daily_trades,
        max_daily_loss=settings.executor_max_daily_loss,
        min_platform_balance=settings.executor_min_platform_balance,
    )

    telegram = TelegramNotifier(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )

    # Connectors are created in main()
    return risk_manager, telegram


async def _init_executor(
    risk_manager: RiskManager,
    telegram: TelegramNotifier,
    poly_connector,
    kalshi_connector,
) -> Executor:
    """Initialize executor with connectors."""
    position_manager = PositionManager()
    await position_manager.connect()

    order_placer = OrderPlacer(
        poly_connector=poly_connector,
        kalshi_connector=kalshi_connector,
    )

    return Executor(
        risk_manager=risk_manager,
        order_placer=order_placer,
        position_manager=position_manager,
        telegram=telegram,
        poly_connector=poly_connector,
        kalshi_connector=kalshi_connector,
    )
```

In the main scan loop, after saving opportunities, add:

```python
# Auto-execute if executor is enabled
if executor and opp.roi_after_fees >= settings.executor_min_roi:
    try:
        await executor.try_execute(opp)
    except Exception as e:
        logger.error(f"Executor error: {e}")
```

**Step 4: Run all tests**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/main.py tests/test_main_executor_integration.py
git commit -m "feat: integrate executor with main scan loop"
```

---

## Task 12: Add Telegram Bot Commands

**Files:**
- Modify: `src/executor/telegram_bot.py`
- Modify: `src/main.py`

**Step 1: Add command handlers to telegram_bot.py**

```python
    async def setup_commands(self, executor: "Executor") -> None:
        """Setup bot command handlers."""
        self._executor = executor

        self._app = Application.builder().token(self.bot_token).build()

        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("stop", self._cmd_stop))
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("trades", self._cmd_trades))
        self._app.add_handler(CommandHandler("pnl", self._cmd_pnl))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command."""
        stats = self._executor.risk.get_stats()

        try:
            poly_bal = await self._executor.poly.get_balance()
            kalshi_bal = await self._executor.kalshi.get_balance()
        except Exception:
            poly_bal = 0
            kalshi_bal = 0

        positions = await self._executor.positions.get_open_positions()

        msg = (
            "📊 <b>STATUS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Enabled: {'✅' if stats['enabled'] else '❌'}\n"
            f"Daily trades: {stats['daily_trades']}/{stats['limits']['max_daily_trades']}\n"
            f"Daily P&L: ${stats['daily_pnl']:+.2f}\n"
            f"Open positions: {len(positions)}\n\n"
            f"💰 Balances:\n"
            f"  Poly: ${poly_bal:.2f}\n"
            f"  Kalshi: ${kalshi_bal:.2f}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /stop command."""
        self._executor.risk.enabled = False
        await update.message.reply_text("🛑 Auto-trading STOPPED")

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        self._executor.risk.enabled = True
        await update.message.reply_text("✅ Auto-trading STARTED")

    async def _cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /trades command - show recent trades."""
        positions = await self._executor.positions.get_open_positions()

        if not positions:
            await update.message.reply_text("No open positions")
            return

        msg = "📜 <b>OPEN POSITIONS</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        for pos in positions[:10]:
            msg += f"• {pos.event_title}\n  ROI: {pos.expected_roi:.1f}%\n"

        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /pnl command."""
        stats = await self._executor.positions.get_daily_stats()

        msg = (
            "💰 <b>P&L SUMMARY</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Today: ${stats['pnl']:+.2f}\n"
            f"Trades: {stats['trades']}\n"
            f"Settled: {stats['settled']}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")
```

**Step 2: Run tests**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS

**Step 3: Commit**

```bash
git add src/executor/telegram_bot.py src/main.py
git commit -m "feat(telegram): add bot commands (/status, /stop, /start, /trades, /pnl)"
```

---

## Task 13: Final Integration Test

**Step 1: Run all tests**

```bash
uv run pytest tests/ -v
```

Expected: All tests PASS

**Step 2: Test with dry run**

Create test script `scripts/test_executor_dry.py`:

```python
"""Dry run test for executor."""

import asyncio
from src.config import settings
from src.executor import RiskManager, TelegramNotifier

async def main():
    print("Testing executor components...")

    # Test risk manager
    rm = RiskManager()
    print(f"Risk manager: enabled={rm.enabled}")
    print(f"Limits: bet=${rm.min_bet}-${rm.max_bet}, ROI={rm.min_roi}%+")

    # Test telegram (if configured)
    if settings.telegram_bot_token:
        tg = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        await tg.send("🧪 Test message from executor")
        print("Telegram: OK")
    else:
        print("Telegram: not configured")

    print("\nDry run complete!")

if __name__ == "__main__":
    asyncio.run(main())
```

Run: `uv run python scripts/test_executor_dry.py`

**Step 3: Commit**

```bash
git add scripts/test_executor_dry.py
git commit -m "test: add executor dry run script"
```

---

## Summary

After completing all tasks, you will have:

1. ✅ Dependencies: `py-clob-client`, `python-telegram-bot`
2. ✅ Config: Executor and Telegram settings
3. ✅ Models: `ExecutionResult`, `LegResult`, `OpenPosition`, `RiskCheckResult`
4. ✅ Risk Manager: Pre-trade validation with limits
5. ✅ Kalshi Trading: `get_balance()`, `place_order()`, `get_order()`
6. ✅ Polymarket Trading: `get_balance()`, `place_order()`, `place_market_order()`
7. ✅ Telegram Notifier: Alerts and bot commands
8. ✅ Order Placer: Parallel leg execution
9. ✅ Position Manager: Track open positions and P&L
10. ✅ Main Executor: Orchestration
11. ✅ Integration: Auto-execute in main loop
12. ✅ Bot Commands: `/status`, `/stop`, `/start`, `/trades`, `/pnl`

**To enable:**

```bash
# .env
EXECUTOR_ENABLED=true
POLY_PRIVATE_KEY=your_private_key
POLY_FUNDER_ADDRESS=your_wallet_address
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

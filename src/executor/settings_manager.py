"""Executor settings manager with memory cache and SQLite persistence."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.db import Database

logger = logging.getLogger(__name__)


@dataclass
class ExecutorSettings:
    """Executor configuration settings."""

    enabled: bool = False
    min_bet: float = 5.0
    max_bet: float = 10.0
    min_roi: float = 1.0
    max_roi: float = 50.0
    max_daily_trades: int = 50
    max_daily_loss: float = 5.0

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "enabled": self.enabled,
            "min_bet": self.min_bet,
            "max_bet": self.max_bet,
            "min_roi": self.min_roi,
            "max_roi": self.max_roi,
            "max_daily_trades": self.max_daily_trades,
            "max_daily_loss": self.max_daily_loss,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ExecutorSettings:
        """Create from dictionary."""
        return cls(
            enabled=bool(data.get("enabled", False)),
            min_bet=float(data.get("min_bet", 5.0)),
            max_bet=float(data.get("max_bet", 10.0)),
            min_roi=float(data.get("min_roi", 1.0)),
            max_roi=float(data.get("max_roi", 50.0)),
            max_daily_trades=int(data.get("max_daily_trades", 50)),
            max_daily_loss=float(data.get("max_daily_loss", 5.0)),
        )


class ExecutorSettingsManager:
    """Manages executor settings with memory cache and DB persistence.

    Thread-safe operations with async lock for concurrent access.
    """

    def __init__(self, db: Database):
        self._db = db
        self._cache: ExecutorSettings | None = None
        self._lock = asyncio.Lock()
        self._subscribers: list[callable] = []

    async def load(self) -> ExecutorSettings:
        """Load settings from DB into cache."""
        async with self._lock:
            data = await self._db.get_executor_settings()
            self._cache = ExecutorSettings.from_dict(data)
            logger.info(f"Loaded executor settings: enabled={self._cache.enabled}")
            return self._cache

    def get(self) -> ExecutorSettings:
        """Get cached settings (sync, fast read).

        Returns default settings if cache not loaded yet.
        """
        if self._cache is None:
            return ExecutorSettings()
        return self._cache

    async def update(self, **kwargs) -> ExecutorSettings:
        """Update settings atomically.

        Args:
            **kwargs: Settings fields to update (enabled, min_bet, max_bet, etc.)

        Returns:
            Updated settings.
        """
        async with self._lock:
            # Update DB
            await self._db.update_executor_settings(**kwargs)

            # Update cache
            if self._cache is None:
                self._cache = ExecutorSettings()

            for key, value in kwargs.items():
                if hasattr(self._cache, key):
                    setattr(self._cache, key, value)

            logger.info(f"Updated executor settings: {kwargs}")

            # Notify subscribers
            await self._notify_subscribers()

            return self._cache

    async def set_enabled(self, enabled: bool) -> None:
        """Enable or disable executor."""
        await self.update(enabled=enabled)

    async def toggle_enabled(self) -> bool:
        """Toggle executor enabled state. Returns new state."""
        current = self.get().enabled
        await self.set_enabled(not current)
        return not current

    def subscribe(self, callback: callable) -> None:
        """Subscribe to settings changes.

        Callback signature: async def callback(settings: ExecutorSettings)
        """
        self._subscribers.append(callback)

    def unsubscribe(self, callback: callable) -> None:
        """Unsubscribe from settings changes."""
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    async def _notify_subscribers(self) -> None:
        """Notify all subscribers of settings change."""
        settings = self.get()
        for callback in self._subscribers:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(settings)
                else:
                    callback(settings)
            except Exception as e:
                logger.error(f"Error notifying subscriber: {e}")

    @property
    def enabled(self) -> bool:
        """Quick check if executor is enabled."""
        return self.get().enabled

"""Integration tests for executor in main loop."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def test_create_executor_when_disabled():
    """Executor should not be created when EXECUTOR_ENABLED=False."""
    with patch("src.main.settings") as mock_settings:
        mock_settings.executor_enabled = False

        from src.main import _create_executor
        result = _create_executor()

        assert result is None


def test_create_executor_when_missing_poly_key():
    """Executor should not be created when POLY_PRIVATE_KEY is empty."""
    with patch("src.main.settings") as mock_settings:
        mock_settings.executor_enabled = True
        mock_settings.poly_private_key = ""

        from src.main import _create_executor
        result = _create_executor()

        assert result is None

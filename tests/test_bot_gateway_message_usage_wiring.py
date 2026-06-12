"""Source-inspection tests for bot_gateway usage_recorder wiring — Story 14.03.

Verifies that the module-level singleton exists and that the startup/shutdown
hooks are registered without running the FastAPI lifecycle (which would require
a real Telegram bot token).
"""
from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.bot_gateway.app.main as bot_main
from services.api.app.usage.recorder import UsageRecorder

_MAIN_SRC = Path(bot_main.__file__).read_text()


class TestUsageRecorderSingleton:
    def test_usage_recorder_is_module_attribute(self):
        assert hasattr(bot_main, "usage_recorder")

    def test_usage_recorder_is_usage_recorder_instance(self):
        assert isinstance(bot_main.usage_recorder, UsageRecorder)


class TestStartupHook:
    def test_start_usage_recorder_hook_defined(self):
        assert hasattr(bot_main, "_start_usage_recorder_on_startup")
        assert callable(bot_main._start_usage_recorder_on_startup)

    def test_start_hook_calls_start(self):
        src = inspect.getsource(bot_main._start_usage_recorder_on_startup)
        assert "usage_recorder.start()" in src


class TestShutdownHook:
    def test_stop_usage_recorder_hook_defined(self):
        assert hasattr(bot_main, "_stop_usage_recorder_on_shutdown")
        assert callable(bot_main._stop_usage_recorder_on_shutdown)

    def test_stop_hook_calls_aclose(self):
        src = inspect.getsource(bot_main._stop_usage_recorder_on_shutdown)
        assert "usage_recorder.aclose()" in src


class TestBootstrapCall:
    def test_bootstrap_usage_db_called_in_module(self):
        assert "bootstrap_usage_db(settings.usage_db_path)" in _MAIN_SRC


class TestHookExecution:
    @pytest.mark.asyncio
    async def test_start_hook_calls_start_on_recorder(self):
        mock_recorder = MagicMock(spec=UsageRecorder)
        with patch.object(bot_main, "usage_recorder", mock_recorder):
            await bot_main._start_usage_recorder_on_startup()
        mock_recorder.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_hook_calls_aclose_on_recorder(self):
        mock_recorder = MagicMock(spec=UsageRecorder)
        mock_recorder.aclose = AsyncMock()
        with patch.object(bot_main, "usage_recorder", mock_recorder):
            await bot_main._stop_usage_recorder_on_shutdown()
        mock_recorder.aclose.assert_awaited_once()

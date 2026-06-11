"""Tests that main.py wires UsageRecorder startup/shutdown and recorder propagation (Story 14.02).

Verifies source-level wiring rather than exercising the live event hooks, so we
don't need a running FastAPI app.  The tests are intentionally narrow: they check
that the key symbols are present in the module source so a reviewer deleting the
wiring will see a failing test.
"""

from __future__ import annotations

import inspect


def _main_source() -> str:
    from services.api.app import main as api_main
    return inspect.getsource(api_main)


def test_usage_recorder_imported_in_main():
    assert "UsageRecorder" in _main_source()


def test_usage_llm_call_repo_imported_in_main():
    assert "UsageLlmCallRepository" in _main_source()


def test_usage_recorder_started_in_startup_hook():
    source = _main_source()
    assert "start_usage_recorder_on_startup" in source
    assert "usage_recorder.start()" in source


def test_usage_recorder_closed_in_shutdown_hook():
    source = _main_source()
    assert "stop_usage_recorder_on_shutdown" in source
    assert "usage_recorder.aclose()" in source


def test_recorder_wired_into_grounded_rag_answerer():
    source = _main_source()
    assert "recorder=usage_recorder" in source


def test_recorder_wired_into_openrouter_client():
    source = _main_source()
    assert "openrouter_client._recorder = usage_recorder" in source

"""Tests for usage_formatter.py (Story 14.08)."""
from __future__ import annotations

from services.bot_gateway.app.usage_formatter import format_degraded, format_usage


def _llm_row(
    *,
    model_name: str = "claude-haiku-4-5",
    prompt_tokens: int = 1000,
    completion_tokens: int = 500,
    cost_usd: float | None = 0.05,
    call_count: int = 3,
) -> dict:
    return {
        "tracker_type": "llm",
        "model_name": model_name,
        "prompt_tokens_total": prompt_tokens,
        "completion_tokens_total": completion_tokens,
        "cost_usd_total": cost_usd,
        "wasted_cost_usd": None,
        "call_count": call_count,
        "in_count": None,
        "out_count": None,
        "hitl_created_count": None,
        "hitl_assigned_count": None,
        "hitl_replied_count": None,
        "hitl_resolved_count": None,
    }


def _msg_row(*, in_count: int = 10, out_count: int = 8) -> dict:
    return {
        "tracker_type": "messages",
        "model_name": "",
        "prompt_tokens_total": None,
        "completion_tokens_total": None,
        "cost_usd_total": None,
        "wasted_cost_usd": None,
        "call_count": None,
        "in_count": in_count,
        "out_count": out_count,
        "hitl_created_count": None,
        "hitl_assigned_count": None,
        "hitl_replied_count": None,
        "hitl_resolved_count": None,
    }


def _hitl_row(
    *, created: int = 2, assigned: int = 1, replied: int = 1, resolved: int = 0
) -> dict:
    return {
        "tracker_type": "hitl",
        "model_name": "",
        "prompt_tokens_total": None,
        "completion_tokens_total": None,
        "cost_usd_total": None,
        "wasted_cost_usd": None,
        "call_count": None,
        "in_count": None,
        "out_count": None,
        "hitl_created_count": created,
        "hitl_assigned_count": assigned,
        "hitl_replied_count": replied,
        "hitl_resolved_count": resolved,
    }


_SAMPLE_ROWS = [_llm_row(), _msg_row(), _hitl_row()]
_WASTED_ROWS = [
    {
        **_llm_row(),
        "wasted_cost_usd": 0.01,
    }
]
_DEEP_LINK = "http://localhost:8001/admin/usage?project_id=1&window=1d"


def test_admin_format_contains_cost():
    text = format_usage(
        summary_rows=_SAMPLE_ROWS,
        wasted_rows=_WASTED_ROWS,
        scope="admin",
        project_name="Тест",
        deep_link=_DEEP_LINK,
    )
    assert "$" in text
    assert "Расход" in text
    assert "потрачено впустую" in text.lower()


def test_operator_format_byte_clean():
    text = format_usage(
        summary_rows=_SAMPLE_ROWS,
        wasted_rows=None,
        scope="operator",
        project_name="Тест",
        deep_link=_DEEP_LINK,
    )
    assert "$" not in text
    assert "Расход" not in text
    assert "Потрачено впустую" not in text


def test_operator_format_shows_tokens_and_calls():
    text = format_usage(
        summary_rows=_SAMPLE_ROWS,
        wasted_rows=None,
        scope="operator",
        project_name="Тест",
        deep_link=_DEEP_LINK,
    )
    assert "prompt" in text.lower()
    assert "completion" in text.lower()
    assert "Вызовов" in text


def test_empty_state_returned_when_all_zero():
    zero_rows = [
        _llm_row(prompt_tokens=0, completion_tokens=0, cost_usd=0.0, call_count=0),
        _msg_row(in_count=0, out_count=0),
        _hitl_row(created=0, assigned=0, replied=0, resolved=0),
    ]
    text = format_usage(
        summary_rows=zero_rows,
        wasted_rows=None,
        scope="admin",
        project_name="Тест",
        deep_link=_DEEP_LINK,
    )
    assert text == "Сегодня данных пока нет."


def test_empty_state_returned_for_no_rows():
    text = format_usage(
        summary_rows=[],
        wasted_rows=None,
        scope="admin",
        project_name="Тест",
        deep_link=_DEEP_LINK,
    )
    assert text == "Сегодня данных пока нет."


def test_degraded_state_string():
    assert format_degraded() == "Данные использования временно недоступны."


def test_multi_model_truncates_to_top_5():
    rows = [
        _llm_row(model_name=f"model-{i}", call_count=10 - i)
        for i in range(7)
    ]
    text = format_usage(
        summary_rows=rows,
        wasted_rows=None,
        scope="operator",
        project_name="Тест",
        deep_link=_DEEP_LINK,
    )
    assert "(и ещё 2)" in text
    # Only top 5 model names should appear explicitly
    assert "model-0" in text
    assert "model-4" in text
    # model-5 and model-6 collapsed into overflow
    assert "model-5" not in text
    assert "model-6" not in text


def test_deep_link_present_in_output():
    text = format_usage(
        summary_rows=_SAMPLE_ROWS,
        wasted_rows=None,
        scope="operator",
        project_name="Тест",
        deep_link=_DEEP_LINK,
    )
    assert _DEEP_LINK in text


def test_output_under_telegram_limit():
    # 7 models + all data should still be under 4096 chars
    rows = [_llm_row(model_name=f"very-long-model-name-{i}", call_count=1) for i in range(7)]
    rows += [_msg_row(), _hitl_row()]
    text = format_usage(
        summary_rows=rows,
        wasted_rows=_WASTED_ROWS,
        scope="admin",
        project_name="Test project",
        deep_link=_DEEP_LINK,
    )
    assert len(text) <= 4096


def test_admin_null_cost_shows_em_dash():
    rows = [_llm_row(cost_usd=None)]
    text = format_usage(
        summary_rows=rows,
        wasted_rows=[],
        scope="admin",
        project_name="Тест",
        deep_link=_DEEP_LINK,
    )
    assert "—" in text


def test_token_thousands_separator():
    rows = [_llm_row(prompt_tokens=12345, completion_tokens=6789, call_count=5)]
    text = format_usage(
        summary_rows=rows,
        wasted_rows=None,
        scope="operator",
        project_name="Тест",
        deep_link=_DEEP_LINK,
    )
    assert "12,345" in text
    assert "6,789" in text


def test_zero_tokens_shows_zero():
    rows = [_llm_row(prompt_tokens=0, completion_tokens=0, call_count=3)]
    text = format_usage(
        summary_rows=rows,
        wasted_rows=None,
        scope="operator",
        project_name="Тест",
        deep_link=_DEEP_LINK,
    )
    assert "0" in text


def test_large_cost_uses_comma_format():
    rows = [_llm_row(cost_usd=1500.75, call_count=1)]
    text = format_usage(
        summary_rows=rows,
        wasted_rows=[{**_llm_row(cost_usd=1500.75), "wasted_cost_usd": 1500.75}],
        scope="admin",
        project_name="Тест",
        deep_link=_DEEP_LINK,
    )
    assert "$1,500.75" in text


def test_long_output_truncated_at_4096():
    # 5 models with 800-char names each push the models line alone past 4096
    rows = [
        _llm_row(model_name="x" * 800 + f"-model-{i}", call_count=1)
        for i in range(6)
    ]
    rows += [_msg_row(in_count=999, out_count=999)]
    text = format_usage(
        summary_rows=rows,
        wasted_rows=[{**_llm_row(cost_usd=0.99), "wasted_cost_usd": 0.99}],
        scope="admin",
        project_name="Test",
        deep_link=_DEEP_LINK,
    )
    assert len(text) == 4096
    assert text.endswith("...")

"""Format /usage command output for admin and operator roles (Story 14.08)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_MAX_MODELS = 5
_TELEGRAM_MAX = 4096


@lru_cache(maxsize=1)
def _strings() -> dict:
    path = Path(__file__).resolve().parents[3] / "data" / "russian_usage_strings.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _fmt_tokens(n: int | None) -> str:
    if not n:
        return "0"
    return f"{n:,}"


def _fmt_cost(v: float | None) -> str:
    if v is None:
        return _strings()["null_cost"]
    if v >= 1_000:
        return f"${v:,.2f}"
    return f"${v:.2f}"


def format_usage(
    *,
    summary_rows: list[dict],
    wasted_rows: list[dict] | None,
    scope: str,
    project_name: str,
    deep_link: str,
) -> str:
    """Render a /usage reply for the given scope ('admin' or 'operator').

    Operator output never contains '$', 'Расход', or 'Потрачено впустую'.
    Returns the empty-state string when all trackers have zero activity.
    """
    s = _strings()

    llm_rows = [r for r in summary_rows if r.get("tracker_type") == "llm"]
    msg_rows = [r for r in summary_rows if r.get("tracker_type") == "messages"]
    hitl_rows = [r for r in summary_rows if r.get("tracker_type") == "hitl"]

    prompt_total = sum(r.get("prompt_tokens_total") or 0 for r in llm_rows)
    completion_total = sum(r.get("completion_tokens_total") or 0 for r in llm_rows)
    cost_total = sum(r.get("cost_usd_total") or 0.0 for r in llm_rows) if llm_rows else None
    call_total = sum(r.get("call_count") or 0 for r in llm_rows)

    model_counts: dict[str, int] = {}
    for r in llm_rows:
        m = r.get("model_name") or ""
        if m:
            model_counts[m] = model_counts.get(m, 0) + (r.get("call_count") or 0)
    models_sorted = sorted(model_counts, key=lambda k: -model_counts[k])

    in_total = sum(r.get("in_count") or 0 for r in msg_rows)
    out_total = sum(r.get("out_count") or 0 for r in msg_rows)

    hitl_created = sum(r.get("hitl_created_count") or 0 for r in hitl_rows)
    hitl_assigned = sum(r.get("hitl_assigned_count") or 0 for r in hitl_rows)
    hitl_replied = sum(r.get("hitl_replied_count") or 0 for r in hitl_rows)
    hitl_resolved = sum(r.get("hitl_resolved_count") or 0 for r in hitl_rows)

    if (
        call_total == 0
        and in_total == 0
        and out_total == 0
        and hitl_created == 0
        and hitl_assigned == 0
        and hitl_replied == 0
        and hitl_resolved == 0
    ):
        return s["empty_state"]

    lines: list[str] = [s["header"].format(project_name=project_name), ""]

    if llm_rows or call_total:
        if scope == "admin":
            lines.append(s["llm_section_admin"])
            wasted_total = (
                sum(r.get("wasted_cost_usd") or 0.0 for r in wasted_rows)
                if wasted_rows
                else None
            )
            lines.append(
                s["cost_line"].format(
                    cost=_fmt_cost(cost_total),
                    wasted=_fmt_cost(wasted_total),
                )
            )
        else:
            lines.append(s["llm_section_operator"])

        lines.append(
            s["tokens_line"].format(
                prompt=_fmt_tokens(prompt_total),
                completion=_fmt_tokens(completion_total),
            )
        )
        lines.append(s["calls_line"].format(count=call_total))

        if models_sorted:
            top = models_sorted[:_MAX_MODELS]
            overflow = len(models_sorted) - _MAX_MODELS
            model_parts = [f"{m} ({model_counts[m]})" for m in top]
            if overflow > 0:
                model_parts.append(s["models_overflow"].format(n=overflow))
            lines.append(s["models_line"].format(models=", ".join(model_parts)))

        lines.append("")

    if msg_rows or in_total or out_total:
        lines.append(
            s["messages_section"].format(in_count=in_total, out_count=out_total)
        )
        lines.append("")

    if hitl_rows or hitl_created or hitl_assigned or hitl_replied or hitl_resolved:
        lines.append(
            s["hitl_section"].format(
                created=hitl_created,
                assigned=hitl_assigned,
                replied=hitl_replied,
                resolved=hitl_resolved,
            )
        )
        lines.append("")

    lines.append(s["deep_link_line"].format(url=deep_link))

    text = "\n".join(lines)
    if len(text) > _TELEGRAM_MAX:
        text = text[: _TELEGRAM_MAX - 3] + "..."
    return text


def format_degraded() -> str:
    return _strings()["degraded_state"]

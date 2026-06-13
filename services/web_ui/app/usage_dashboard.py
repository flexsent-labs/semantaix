"""Usage dashboard page — Story 14.06.

Route: GET /admin/usage   — main dashboard
Route: GET /admin/usage/raw — drill-down JSON for a specific day+tracker

Auth: uses the semantaix_session cookie (same mechanism as /files).
Reads repos directly following the existing admin-shell pattern.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from html import escape as _esc
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from platform_common.settings import get_settings
from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    UsageDailySummaryRepository,
    UsageDailySummaryRow,
    UsageHitlEventRepository,
    UsageLlmCallRepository,
    UsageMessageRepository,
)
from services.web_ui.app.auth import _resolve_principal

router = APIRouter()
_settings = get_settings()
_MAX_WINDOW_DAYS = _settings.usage_raw_retention_days


def _window_to_dates(window: str, today: date) -> tuple[date, date]:
    """Convert a window string to (from_date, to_date) in UTC."""
    yesterday = today - timedelta(days=1)
    if window == "1d":
        return yesterday, yesterday
    if window == "1w":
        return today - timedelta(days=7), yesterday
    return today - timedelta(days=_MAX_WINDOW_DAYS), yesterday


def _parse_custom_dates(
    from_str: str, to_str: str, today: date
) -> tuple[date, date, bool]:
    """Return (from_date, to_date, capped) from custom range strings.

    Caps the range at MAX_WINDOW_DAYS and returns whether it was capped.
    """
    try:
        from_date = date.fromisoformat(from_str)
        to_date = date.fromisoformat(to_str)
    except (ValueError, TypeError):
        yesterday = today - timedelta(days=1)
        return today - timedelta(days=_MAX_WINDOW_DAYS), yesterday, False
    capped = False
    if (to_date - from_date).days >= _MAX_WINDOW_DAYS:
        from_date = to_date - timedelta(days=_MAX_WINDOW_DAYS - 1)
        capped = True
    return from_date, to_date, capped


def local_window_to_utc_range(
    window: str, local_date: date, utc_offset_minutes: int
) -> tuple[date, date]:
    """Compute the UTC date range for a window given a browser local date and TZ offset.

    utc_offset_minutes: minutes AHEAD of UTC (positive = east, e.g. +180 for MSK).
    This is the inverse of JS Date.getTimezoneOffset() which returns minutes BEHIND UTC.
    """
    local_midnight = datetime(local_date.year, local_date.month, local_date.day, 0, 0, 0)
    utc_midnight = local_midnight - timedelta(minutes=utc_offset_minutes)
    utc_today = utc_midnight.date()
    return _window_to_dates(window, utc_today)


def _aggregate_tiles(
    rows: list[UsageDailySummaryRow],
) -> dict[str, Any]:
    """Compute tile totals from summary rows."""
    llm_cost = 0.0
    llm_tokens = 0
    llm_wasted = 0.0
    msg_count = 0
    hitl_count = 0
    for r in rows:
        if r.tracker_type == "llm":
            llm_cost += r.cost_usd_total or 0.0
            llm_tokens += (r.prompt_tokens_total or 0) + (r.completion_tokens_total or 0)
            llm_wasted += r.wasted_cost_usd or 0.0
        elif r.tracker_type == "messages":
            msg_count += r.call_count or 0
        elif r.tracker_type == "hitl":
            hitl_count += r.call_count or 0
    return {
        "llm_cost": round(llm_cost, 4),
        "llm_tokens": llm_tokens,
        "llm_wasted": round(llm_wasted, 4),
        "msg_count": msg_count,
        "hitl_count": hitl_count,
    }


def _series_for_charts(
    rows: list[UsageDailySummaryRow], from_date: date, to_date: date
) -> dict[str, Any]:
    """Build day-indexed series for chart rendering."""
    days = []
    d = from_date
    while d <= to_date:
        days.append(d.isoformat())
        d += timedelta(days=1)

    llm_cost_by_day: dict[str, float] = {day: 0.0 for day in days}
    msg_in_by_day: dict[str, int] = {day: 0 for day in days}
    msg_out_by_day: dict[str, int] = {day: 0 for day in days}
    hitl_by_day: dict[str, int] = {day: 0 for day in days}

    for r in rows:
        if r.day_utc not in llm_cost_by_day:
            continue
        if r.tracker_type == "llm":
            llm_cost_by_day[r.day_utc] = round(
                llm_cost_by_day[r.day_utc] + (r.cost_usd_total or 0.0), 4
            )
        elif r.tracker_type == "messages":
            msg_in_by_day[r.day_utc] += r.in_count or 0
            msg_out_by_day[r.day_utc] += r.out_count or 0
        elif r.tracker_type == "hitl":
            hitl_by_day[r.day_utc] += r.call_count or 0

    return {
        "days": days,
        "llm_cost": [llm_cost_by_day[d] for d in days],
        "msg_in": [msg_in_by_day[d] for d in days],
        "msg_out": [msg_out_by_day[d] for d in days],
        "hitl": [hitl_by_day[d] for d in days],
    }


def _sparkline_svg(values: list[float | int], *, width: int = 120, height: int = 40) -> str:
    """Render a tiny SVG sparkline from a list of values."""
    if not values or max(values) == 0:
        return (
            f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
            f'<text x="4" y="{height - 4}" font-size="10" fill="#999">—</text></svg>'
        )
    mn, mx = min(values), max(values)
    rng = mx - mn or 1
    n = len(values)
    step = width / max(n - 1, 1)
    pts = " ".join(
        f"{i * step:.1f},{height - 4 - (v - mn) / rng * (height - 8):.1f}"
        for i, v in enumerate(values)
    )
    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f'<polyline points="{pts}" fill="none" stroke="#4a9eff" stroke-width="1.5"/>'
        f'</svg>'
    )


def _render_tile(
    label: str,
    value: str,
    sparkline_svg: str,
    *,
    highlight: bool = False,
) -> str:
    border = "border:2px solid #e05;" if highlight else "border:1px solid #ccc;"
    return (
        f"<div style='display:inline-block;padding:12px 16px;margin:6px;"
        f"vertical-align:top;min-width:160px;{border}border-radius:6px;"
        f"background:#fafafa'>"
        f"<div style='font-size:12px;color:#666'>{_esc(label)}</div>"
        f"<div style='font-size:22px;font-weight:bold;margin:4px 0'>{value}</div>"
        f"{sparkline_svg}</div>"
    )


_DASHBOARD_JS = """
<script>
// Browser-timezone day labels
function localDayLabel(dayUtc) {
  const d = new Date(dayUtc + 'T12:00:00Z');
  return d.toLocaleDateString(undefined, {month:'short', day:'numeric'});
}

// Minimal SVG line chart
function lineChart(container, days, series, labels, colors) {
  const W = container.offsetWidth || 400, H = 140, pad = 32;
  const allVals = series.flat();
  const mn = 0, mx = Math.max(...allVals, 0.01);
  const xs = days.map((_, i) => pad + i * (W - pad * 2) / Math.max(days.length - 1, 1));
  const y = v => H - pad - (v - mn) / (mx - mn) * (H - pad * 2);

  const ns = 'http://www.w3.org/2000/svg';
  let svg = `<svg width="${W}" height="${H}" xmlns="${ns}" style="display:block">`;
  // grid lines
  for (let i = 0; i <= 4; i++) {
    const yy = pad + i * (H - pad * 2) / 4;
    svg += `<line x1="${pad}" y1="${yy}" x2="${W-pad}" y2="${yy}"`;
    svg += ` stroke="#eee" stroke-width="1"/>`;
  }
  // series
  series.forEach((vals, si) => {
    const pts = xs.map((x, i) => `${x.toFixed(1)},${y(vals[i]).toFixed(1)}`).join(' ');
    svg += `<polyline points="${pts}" fill="none" stroke="${colors[si]}" stroke-width="2"/>`;
    // dots
    vals.forEach((v, i) => {
      svg += `<circle cx="${xs[i].toFixed(1)}" cy="${y(v).toFixed(1)}" r="3"
        fill="${colors[si]}" class="chart-dot"
        data-day="${days[i]}" data-val="${v}" data-label="${labels[si]}"/>`;
    });
  });
  // x-axis labels (every 3rd day or fewer)
  const step = Math.max(1, Math.floor(days.length / 8));
  days.forEach((d, i) => {
    if (i % step === 0 || i === days.length - 1) {
      svg += `<text x="${xs[i].toFixed(1)}" y="${H - 4}" text-anchor="middle"
        font-size="9" fill="#888">${localDayLabel(d)}</text>`;
    }
  });
  svg += '</svg>';
  container.innerHTML = svg;

  // Drill-down on dot click
  container.querySelectorAll('.chart-dot').forEach(dot => {
    dot.style.cursor = 'pointer';
    dot.addEventListener('click', () => {
      const day = dot.dataset.day;
      const tracker = container.dataset.tracker;
      openDrillDown(day, tracker);
    });
  });
}

// Drill-down panel
function openDrillDown(day, tracker) {
  const panel = document.getElementById('drill-down-panel');
  const title = document.getElementById('drill-down-title');
  const body = document.getElementById('drill-down-body');
  const projectId = document.getElementById('dash-data').dataset.projectId;
  title.textContent = `Raw rows — ${day} / ${tracker}`;
  body.textContent = 'Loading…';
  panel.style.display = 'block';
  fetch(`/admin/usage/raw?project_id=${projectId}&day_utc=${day}&tracker_type=${tracker}&page=1`)
    .then(r => r.json())
    .then(data => {
      if (data.unavailable) {
        body.innerHTML = '<em>' + data.message + '</em>';
        return;
      }
      let html = `<table border="1" cellpadding="4"><thead><tr>`;
      if (data.rows.length > 0) {
        Object.keys(data.rows[0]).forEach(k => { html += `<th>${k}</th>`; });
        html += '</tr></thead><tbody>';
        data.rows.forEach(row => {
          html += '<tr>';
          Object.values(row).forEach(v => { html += `<td>${v ?? '—'}</td>`; });
          html += '</tr>';
        });
        html += '</tbody></table>';
        if (data.has_more) {
          const nextP = data.page + 1;
          html += `<button onclick="loadMore('${day}','${tracker}',${nextP})">Load more</button>`;
        }
      } else {
        html = '<em>No raw rows for this day.</em>';
      }
      body.innerHTML = html;
    })
    .catch(() => { body.textContent = 'Failed to load drill-down data.'; });
}

function loadMore(day, tracker, page) {
  const body = document.getElementById('drill-down-body');
  const projectId = document.getElementById('dash-data').dataset.projectId;
  fetch(`/admin/usage/raw?project_id=${projectId}&day_utc=${day}&tracker_type=${tracker}&page=${page}`)
    .then(r => r.json())
    .then(data => {
      const tbl = body.querySelector('table');
      const tbody = tbl && tbl.querySelector('tbody');
      if (tbody && data.rows.length > 0) {
        data.rows.forEach(row => {
          const tr = document.createElement('tr');
          Object.values(row).forEach(v => {
            const td = document.createElement('td');
            td.textContent = v ?? '—';
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
      }
      const btn = body.querySelector('button');
      if (btn) btn.remove();
      if (data.has_more && data.rows.length > 0) {
        body.insertAdjacentHTML('beforeend',
          `<button onclick="loadMore('${day}','${tracker}',${data.page+1})">Load more</button>`);
      }
    });
}

function closeDrillDown() {
  document.getElementById('drill-down-panel').style.display = 'none';
}

document.addEventListener('DOMContentLoaded', () => {
  const el = document.getElementById('dash-data');
  if (!el) return;
  const chartData = JSON.parse(el.dataset.charts);
  const isAdmin = el.dataset.isAdmin === 'true';

  const llmDiv = document.getElementById('chart-llm');
  const msgDiv = document.getElementById('chart-messages');
  const hitlDiv = document.getElementById('chart-hitl');

  if (llmDiv) lineChart(llmDiv, chartData.days, [chartData.llm_cost], ['cost USD'], ['#4a9eff']);
  if (msgDiv) lineChart(msgDiv, chartData.days, [chartData.msg_in, chartData.msg_out],
    ['in', 'out'], ['#22aa55', '#ff9922']);
  if (hitlDiv) lineChart(hitlDiv, chartData.days, [chartData.hitl], ['events'], ['#aa44ff']);
});
</script>
"""


def _render_dashboard(
    *,
    principal: dict,
    project_id: int | None,
    window: str,
    from_date: date | None,
    to_date: date | None,
    capped: bool,
    rows: list[UsageDailySummaryRow],
    today: date,
) -> str:
    is_admin = principal.get("role") == "admin"
    username = _esc(str(principal.get("username", "")))
    role = _esc(str(principal.get("role", "")))

    # Determine display window
    if from_date is None or to_date is None:
        f_date, t_date = _window_to_dates(window, today)
    else:
        f_date, t_date = from_date, to_date

    # Time selector
    def _sel(w: str) -> str:
        checked = "checked" if window == w and from_date is None else ""
        return (
            f"<label><input type='radio' name='window' value='{w}' {checked}"
            f" onchange='this.form.submit()'>{_esc(w)}</label> "
        )

    pid_input = (
        f"<input type='hidden' name='project_id' value='{project_id}'/>"
        if project_id else ""
    )
    custom_from = f_date.isoformat() if from_date else ""
    custom_to = t_date.isoformat() if from_date else ""
    custom_checked = "checked" if from_date else ""

    cap_notice = (
        "<p style='color:#c80'><strong>Диапазон ограничен 30 днями.</strong></p>"
        if capped else ""
    )

    time_selector = f"""
    <form method='get' action='/admin/usage' style='margin-bottom:12px'>
      {pid_input}
      {_sel("1d")}{_sel("1w")}{_sel("1m")}
      <label><input type='radio' name='window' value='custom' {custom_checked}
        onchange='this.form.submit()'>Дата</label>
      <input type='date' name='from' value='{custom_from}'
             max='{today.isoformat()}' onchange='this.form.submit()' />
      &nbsp;—&nbsp;
      <input type='date' name='to' value='{custom_to}'
             max='{today.isoformat()}' onchange='this.form.submit()' />
      <button type='submit'>Применить</button>
    </form>
    {cap_notice}
    """

    _empty = not rows
    if _empty:
        charts_json = json.dumps(
            {"days": [], "llm_cost": [], "msg_in": [], "msg_out": [], "hitl": []}
        )
        _no_data = (
            "<div style='color:#999'><em>"
            "Нет данных. Активность появится после первого ролла."
            "</em></div>"
        )
        llm_tile = _render_tile("LLM (стоимость)", "—", _no_data)
        msg_tile = _render_tile("Сообщения", "—", _no_data)
        hitl_tile = _render_tile("HITL события", "—", _no_data)
        wasted_tile = (
            _render_tile("Потрачено впустую", "—", _no_data, highlight=True)
            if is_admin else ""
        )
        tiles_html = (
            "<div style='margin-bottom:16px'>"
            + llm_tile + msg_tile + hitl_tile + wasted_tile
            + "</div>"
        )
        chart_html = ""
    else:
        tiles = _aggregate_tiles(rows)
        series = _series_for_charts(rows, f_date, t_date)
        charts_json = json.dumps(series)

        llm_svg = _sparkline_svg(series["llm_cost"])
        msg_svg = _sparkline_svg([a + b for a, b in zip(series["msg_in"], series["msg_out"])])
        hitl_svg = _sparkline_svg(series["hitl"])

        llm_tile = _render_tile(
            "LLM (стоимость)", f"${tiles['llm_cost']:.4f}", llm_svg
        )
        msg_tile = _render_tile("Сообщения", str(tiles["msg_count"]), msg_svg)
        hitl_tile = _render_tile("HITL события", str(tiles["hitl_count"]), hitl_svg)

        wasted_tile = ""
        if is_admin:
            wasted_tile = _render_tile(
                "Потрачено впустую",
                f"${tiles['llm_wasted']:.4f}",
                _sparkline_svg([r.wasted_cost_usd or 0.0 for r in rows
                                 if r.tracker_type == "llm"]),
                highlight=True,
            )

        tiles_html = (
            "<div style='margin-bottom:16px'>"
            + llm_tile + msg_tile + hitl_tile + wasted_tile
            + "</div>"
        )

        chart_html = """
        <h2>Расходы LLM (USD/день)</h2>
        <div id='chart-llm' data-tracker='llm' style='width:100%;max-width:700px'></div>
        <h2>Объём сообщений</h2>
        <div id='chart-messages' data-tracker='messages' style='width:100%;max-width:700px'></div>
        <h2>HITL события</h2>
        <div id='chart-hitl' data-tracker='hitl' style='width:100%;max-width:700px'></div>
        """

    proj_info = f"<p>Проект: <strong>{project_id}</strong></p>" if project_id else (
        "<p style='color:#c00'>Укажите <code>?project_id=&lt;id&gt;</code>"
        " в URL для просмотра данных.</p>"
    )

    is_admin_js = "true" if is_admin else "false"

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Использование — Semantaix</title>
  <style>
    body {{ font-family: sans-serif; padding: 16px; }}
    nav {{ margin-bottom: 16px; }}
    h1 {{ margin: 0 0 8px 0; }}
  </style>
</head>
<body>
  <p style='float:right'>Signed in as <strong>{username}</strong> ({role}) ·
    <form action='/logout' method='post' style='display:inline'>
      <button type='submit'>Logout</button>
    </form>
  </p>
  <h1>Использование</h1>
  {proj_info}
  {time_selector}
  {tiles_html}
  {chart_html}

  <div id='drill-down-panel'
    style='display:none;margin-top:20px;border-top:1px solid #ccc;padding-top:12px'>
    <button onclick="closeDrillDown()">Закрыть</button>
    <h3 id='drill-down-title'></h3>
    <div id='drill-down-body'></div>
  </div>

  <div id='dash-data' style='display:none'
    data-project-id='{project_id or ""}'
    data-is-admin='{is_admin_js}'
    data-charts='{_esc(charts_json)}'></div>

  {_DASHBOARD_JS}
</body>
</html>"""


@router.get("/admin/usage", response_class=HTMLResponse)
async def usage_dashboard(
    request: Request,
    project_id: int | None = None,
    window: str = "1w",
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
) -> Response:
    from fastapi.responses import RedirectResponse

    principal = await _resolve_principal(request)
    if principal is None:
        return RedirectResponse(url="/login", status_code=303)

    today = datetime.now(UTC).date()
    capped = False
    from_date: date | None = None
    to_date: date | None = None

    if from_ and to:
        from_date, to_date, capped = _parse_custom_dates(from_, to, today)
        window = "custom"

    # Summary-only reads for the main page
    bootstrap_usage_db(_settings.usage_db_path)
    summary_repo = UsageDailySummaryRepository(db_path=_settings.usage_db_path)
    is_admin = principal.get("role") == "admin"
    rows: list[UsageDailySummaryRow] = []
    if project_id is not None:
        if from_date is not None and to_date is not None:
            f_date, t_date = from_date, to_date
        else:
            f_date, t_date = _window_to_dates(window, today)
        rows = await asyncio.to_thread(
            summary_repo.query,
            project_id=project_id,
            from_day_utc=f_date.isoformat(),
            to_day_utc=t_date.isoformat(),
            include_money=is_admin,
        )
    else:
        f_date, t_date = None, None

    return HTMLResponse(
        _render_dashboard(
            principal=principal,
            project_id=project_id,
            window=window,
            from_date=from_date,
            to_date=to_date,
            capped=capped,
            rows=rows,
            today=today,
        )
    )


@router.get("/admin/usage/raw")
async def usage_raw(
    request: Request,
    project_id: int,
    day_utc: str,
    tracker_type: str,
    page: int = 1,
    page_size: int = 100,
) -> Response:
    principal = await _resolve_principal(request)
    if principal is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    bootstrap_usage_db(_settings.usage_db_path)
    today = datetime.now(UTC).date()
    try:
        row_date = date.fromisoformat(day_utc)
    except ValueError:
        return JSONResponse({"error": "invalid day_utc"}, status_code=400)

    if (today - row_date).days > _MAX_WINDOW_DAYS:
        return JSONResponse(
            {
                "unavailable": True,
                "message": "Raw data is no longer available (30-day retention)",
            },
            status_code=410,
        )

    if tracker_type == "llm":
        repo: Any = UsageLlmCallRepository(db_path=_settings.usage_db_path)
    elif tracker_type == "messages":
        repo = UsageMessageRepository(db_path=_settings.usage_db_path)
    elif tracker_type == "hitl":
        repo = UsageHitlEventRepository(db_path=_settings.usage_db_path)
    else:
        return JSONResponse({"error": "unknown tracker_type"}, status_code=400)

    is_admin = principal.get("role") == "admin"
    if tracker_type == "llm":
        raw_rows = await asyncio.to_thread(
            repo.list_for_day,
            project_id=project_id,
            day_utc=day_utc,
            page=page,
            page_size=page_size,
            include_money=is_admin,
        )
    else:
        raw_rows = await asyncio.to_thread(
            repo.list_for_day,
            project_id=project_id,
            day_utc=day_utc,
            page=page,
            page_size=page_size,
        )

    from dataclasses import asdict
    serialized = [asdict(r) for r in raw_rows]
    has_more = len(raw_rows) == page_size

    return JSONResponse(
        {"rows": serialized, "page": page, "has_more": has_more, "unavailable": False}
    )

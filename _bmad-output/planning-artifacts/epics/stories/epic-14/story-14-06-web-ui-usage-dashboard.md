# Story 14.06 — Web UI usage dashboard (charts, drill-down, Wasted-spend tile, browser-tz rendering)

## Objective
Ship the single-page Usage dashboard at `/admin/usage`: three tracker tiles, a time selector (1d / 1w / 1m / custom range up to 30 days), line/sparkline charts, per-model LLM breakdown, drill-down to the last-30-day raw call list, and the **Wasted-spend tile** with per-`call_outcome` breakdown. Days rendered in admin browser timezone; storage stays UTC. Reads `usage_daily_summary` first (summary-first to mitigate WAL contention); raw rows only on drill-down click. Empty-state and degraded-state UX both covered.

**As an** admin,
**I want** a single dashboard that shows me LLM spend, message volume, and HITL load over the last day / week / month for a project, with a clear "wasted spend" callout and the ability to drill into individual calls,
**So that** I can understand cost drivers, spot spikes, and trace surprising costs back to specific moments without scrolling logs.

PRD reference: **FR-32** (Usage Dashboard).

## Scope

### In Scope
- **New `web_ui` route** `/admin/usage` (renders the dashboard page; cookie-auth required; reads `Settings`/session for the admin's scope).
- **Dashboard layout** (single page, no tabs):
  - **Time selector** (top bar): radio buttons `1d / 1w / 1m / Custom`; Custom opens two date inputs bounded to ≤ 30 days range; selection persists in the URL querystring.
  - **Three tracker tiles** (top row): "LLM cost (today)" or token count for operators, "Messages (today)", "HITL events (today)". Each tile shows the current-window total + a sparkline of the last 7 daily summaries.
  - **Wasted-spend tile** (admin only): total wasted cost for the window + a horizontal-bar mini-chart with the per-`call_outcome` breakdown (`verifier_rejected`, `guardrails_blocked`, `error`).
  - **LLM cost chart** (main): line chart of daily totals across the window; one line per model when there is more than one. Hovering a point shows the day's totals.
  - **Message-volume chart**: line chart of in/out counts.
  - **HITL events chart**: stacked bar of `created / assigned / replied / resolved` per day.
  - **Drill-down panel** (collapsed by default): clicking a chart point opens the drill-down with the raw rows for that day from the relevant tracker. Drill-down is bounded to the last 30 days (older raw rows are purged); attempting to drill into an older day shows "Raw data is no longer available (30-day retention)".
- **`/api/usage/summary?project_id=&from=&to=&trackers=`** consumed by all charts (one API call per page-load + on selector change). Returns daily summary rows for the window + trackers.
- **`/api/usage/wasted?project_id=&from=&to=`** consumed by the Wasted-spend tile. Admin only; operator scope returns 403.
- **`/api/usage/raw?project_id=&day_utc=&tracker_type=&page=&page_size=`** consumed by the drill-down panel; paginated; returns up to N rows for the requested day + tracker.
- **Summary-first reads** — chart + tile queries hit `/api/usage/summary` (which queries `usage_daily_summary` only); raw rows are queried ONLY on drill-down click. Verified in the API tests via a query-counter assertion: zero reads against the raw tables during a chart render.
- **Browser-timezone rendering** — all `day_utc` values from the API are reinterpreted in JS as the admin's local timezone. The day boundary on the chart matches local midnight, not UTC midnight. The 1d window means "from local midnight today to now"; 1w means "from local midnight 7 days ago"; 1m means "from local midnight 30 days ago". Week buckets start Monday in the local week.
- **Time-range bounding for custom range** — if the admin selects a custom range > 30 days, the dashboard shows a notice "Custom range capped at 30 days" and silently truncates to the last 30 days. (Daily summaries support arbitrary ranges; the 30-day cap is for drill-down consistency — see brainstorm C3.)
- **Empty state** — when `/api/usage/summary` returns zero rows, each tile shows "No data yet" placeholder text + a small explanation line ("Activity for this project will appear here").
- **Degraded state** — when `/api/usage/summary` returns 503 (the api detected `usage.db` corruption), the whole page renders a single banner "Usage data unavailable" + the rest of the admin shell remains usable.
- **Operator scope behavior** — the same page is reachable by operators on their assigned project; the UI hides the Wasted-spend tile and any cost-related elements at render time (the API also strips cost fields per money-RBAC — defense in depth). Operator output shows tokens + counts only.
- **Page state in URL** — `?project_id=&window=1w` (or `from=…&to=…`); shareable, bookmark-friendly.
- **Deep-link from bot** — the `/usage` bot command (14.08) DMs a link of shape `/admin/usage?project_id=<id>&window=1d`; the dashboard initializes from these query params.

### Out of Scope
- The API endpoints themselves (story 14.07 owns `/api/usage/*`). This story consumes them; if they don't exist yet, this story may ship behind a stub-API while 14.07 is in flight, or 14.07 lands first. Acceptable either order — both can proceed in parallel after 14.05.
- The bot `/usage` command (14.08).
- Alerting UI (alerts surface in the existing Epic 02 Alerts tab; no new alerting UI in this story).
- Project picker UI for admins managing multiple projects — admin's "current project" comes from existing admin shell scoping (or, if absent, the URL `project_id` query param is required).
- Chart library upgrade — use whatever chart primitive the existing admin shell uses (or a minimal SVG/Canvas sparkline if none exists). NO heavy chart library imports.
- CSV/PDF export — future epic.
- Real-time refresh — page loads on demand only; user must reload or change selector to update.

## Implementation Notes
- **`services/web_ui/app/usage_dashboard.py`** owns the route + the Jinja2 template (or whatever templating the existing admin shell uses).
- **JS for browser-tz rendering** — small JS module `static/js/usage_dashboard.js` (or `.ts`) that:
  - Takes the API response (UTC `day_utc` strings) and rebuckets them in `Intl.DateTimeFormat`'s local timezone.
  - Computes the local "today / yesterday / N days ago" boundaries from `new Date()` + `Intl`.
  - Renders the charts (one small sparkline module — D3 if already present, or a hand-rolled SVG path).
- **Summary-first verification** — the route handler doesn't import `UsageLlmCallRepository` (raw table repo) at the chart-rendering path; it only imports `UsageDailySummaryRepository` via the api endpoint. Drill-down opens a separate component that fetches `/api/usage/raw` on click.
- **Pagination for drill-down** — default `page_size=100`; "Load more" button fetches the next page until no more rows or 30-day boundary.
- **Empty-state vs degraded-state distinction** — empty-state: API returned 200 with zero rows; degraded-state: API returned 503 OR the response carries a `unavailable: true` flag from the api when it detects DB unavailability.
- **Operator scope detection** — the web_ui route reads the cookie session and checks the user's role (admin vs operator). If operator, render the operator-mode template (no Wasted-spend tile, no cost columns). The API will ALSO strip cost fields (story 14.07's RBAC), so this is defense in depth.
- **Russian labels in UI** — Russian-first per project-context.md, but Web UI labels are configured in `data/web_ui_strings_ru.json` (or wherever the admin shell already holds them). Add the new strings (`"Использование"`, `"Расходы"`, `"Потраченные впустую"`, etc.) to that file. Keep RU + EN parallel where the codebase already does.
- **`Settings`** — no new env vars; reuses `internal_service_token` for API calls only if the web_ui dashboard calls the api over network (otherwise reads the daily-summary repo directly via `to_thread` — match existing admin-shell pattern).

## Test Plan

### Unit
- `tests/test_usage_dashboard_route.py`:
  - GET `/admin/usage` without session → 401 / redirect to login.
  - GET with admin session → 200, page contains the three tracker tile placeholders + Wasted-spend tile + time selector.
  - GET with operator session → 200, page contains tracker tiles WITHOUT the Wasted-spend tile + cost columns hidden.
  - Query param `?window=1w` → page initializes with 1w selected.
  - Query param `?project_id=123` (admin) → page scopes to that project.
- `tests/test_usage_dashboard_summary_first.py`:
  - Mock `/api/usage/summary` to return 5 days of summaries → dashboard renders without ANY call to `/api/usage/raw` (query-counter assertion).
- `tests/test_usage_dashboard_drill_down.py`:
  - Click on a chart point (simulated via API call to `/api/usage/raw?day_utc=2026-05-25&...`) → 200 with paginated rows.
  - Click on a point older than 30 days → 410 (or 200 with `unavailable_reason: 'retention_30_days'`); UI shows "Raw data is no longer available".
- `tests/test_usage_dashboard_empty_state.py`:
  - `/api/usage/summary` returns 0 rows → each tile renders "No data yet" placeholder.
- `tests/test_usage_dashboard_degraded_state.py`:
  - `/api/usage/summary` returns 503 → banner "Usage data unavailable" appears; rest of admin shell unaffected.
- `tests/test_usage_dashboard_browser_tz_bucketing.py`:
  - JS unit test (or Python-side simulation): an admin in `Europe/Moscow` (UTC+3) viewing the 1d window with current local time `2026-05-26 02:00 MSK` sees "today" cut at `2026-05-25 21:00 UTC` (= local midnight); the previous day's evening traffic (UTC 18:00-21:00) appears in TODAY's bucket, not yesterday's.
  - 1w view starts on the Monday of the local week.
- `tests/test_usage_dashboard_custom_range_cap.py`:
  - Custom range > 30 days → notice shown + truncate to last 30 days.

### Contract
- API contracts owned by 14.07; this story tests the consumer side (mock the API).

### Integration
- `tests/test_usage_dashboard_integration.py` — boot the api + web_ui; seed `usage_daily_summary` rows across 7 days; load `/admin/usage?window=1w` → page shows all 7 days of data on each chart.

## Automated E2E verification
- `tests/e2e/test_e2e_epic14_dashboard_render.py` (`@pytest.mark.e2e @pytest.mark.epic("14") @pytest.mark.story("14-06")`):
  - Full stack: send synthetic traffic for a project; run the roll-up; load `/admin/usage?project_id=<id>&window=1d` as admin → assert all three tiles render with non-zero counts + Wasted-spend tile shows the synthetic wasted-cost total.
  - Load as operator (scoped to assigned project) → assert NO `$`-formatted cost text in the rendered HTML.
  - Click a chart point (simulated) → drill-down panel opens with paginated raw rows for that day.
  - Tamper with `usage.db` to be unreadable → page renders the degraded banner instead of erroring.

## Manual Verification
1. `docker compose up --build -d`; let synthetic traffic accumulate for ≥2 days; navigate to `/admin/usage` → confirm three tiles, Wasted-spend tile, sparklines render.
2. Change time selector to 1m → chart x-axis covers 30 days.
3. Click a chart point on yesterday → drill-down panel opens with the rows.
4. Set OS timezone to `Europe/Moscow` (or use a VPN/browser-tz override) → confirm day boundaries match local midnight.
5. Log in as an operator → confirm Wasted-spend tile and cost columns are hidden.

## Done Criteria
- 100% line coverage on the new route + template + JS module (or Python tests that simulate JS bucketing for tz logic).
- `ruff check .` passes.
- Summary-first verified — chart rendering reads only daily summaries.
- Browser-tz rendering verified — local-midnight boundaries match for `Europe/Moscow`.
- Operator-scope rendering verified — no cost data in the DOM.
- Empty-state + degraded-state both render without errors.
- E2E dashboard render green.
- Russian UI labels added to `data/web_ui_strings_ru.json` (per project-context rule).

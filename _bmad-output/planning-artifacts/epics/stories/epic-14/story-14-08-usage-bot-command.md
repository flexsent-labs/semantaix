# Story 14.08 — `/usage` bot command (role-aware, three-tracker output, deep link)

## Objective
Ship the `/usage` Telegram slash command: a single text reply with all three trackers' current-day summaries + a deep link to the Web UI dashboard. Admin output includes cost; operator output strips cost at the API boundary (defense in depth on top of 14.07's money RBAC). Operator-gated via the Epic 10 registry; project resolved via the Epic 10.5 operator-↔-project mapping.

**⚠️ Gated on Epic 10.5 + Story 14.07 shipping first.**

**As an** admin,
**I want** to type `/usage` in Telegram and see today's LLM cost, message volume, and HITL events for my project,
**So that** I can do a quick spend check without opening the dashboard.

**As an** operator,
**I want** to type `/usage` and see today's token + message + HITL counts for my assigned project (no money figures),
**So that** I can gauge load without seeing data the platform considers privileged.

PRD reference: **FR-34** (Role-Aware `/usage` Bot Command).

## Scope

### In Scope
- **`/usage` slash command** in `services/bot_gateway/app/usage_command.py`:
  - Start-of-message anchored regex `^/usage(?:\s+(.+))?$` (optional argument for admins to specify project by name).
  - Operator-gated via Epic 10 registry; non-registered senders ignored with logged `unauthorized_usage` reason, **no DM** (avoids accidental customer-thread reveals).
  - Resolves the sender's role (`admin | operator`) via the existing auth lookup.
  - **For operators**: project resolved via Epic 10.5's single-project-per-operator mapping. If unresolved, DM "у вас не назначен проект — обратитесь к админу" (Russian; configured in data file).
  - **For admins**: project resolved (1) from `/usage <project_name>` argument if present; OR (2) from the chat context (if the admin is currently in a project's chat); OR (3) DM the admin "укажите проект: `/usage <название>`" and list available projects (top 10 by recent activity).
- **`ApiClient.fetch_usage_today(project_id, scope, as_user)`** in `services/bot_gateway/app/api_client.py`:
  - Calls `/api/usage/summary?project_id=&from=<today_local_midnight_utc>&to=<now_utc>&trackers=all` with `internal_service_token` + `as_user=<sender>` header.
  - Returns parsed response. For admin scope, also calls `/api/usage/wasted?project_id=&from=...&to=...`.
- **Formatted Russian text reply** rendered by `services/bot_gateway/app/usage_formatter.py`:
  - **Admin format** (illustrative — copy lives in `data/russian_usage_strings.json`):
    ```
    📊 Использование за сегодня — <project_name>

    💰 LLM:
      • Расход: $X.XX (потрачено впустую: $Y.YY)
      • Токены: NN prompt + NN completion
      • Вызовов: N
      • Модели: claude-haiku-4-5 (N), gpt-4 (N)

    💬 Сообщения: N входящих, N исходящих

    🎫 HITL: N создано, N назначено, N отвечено, N закрыто

    🔗 Подробнее: <deep_link>
    ```
  - **Operator format** — same shape but the LLM block becomes:
    ```
    💬 LLM:
      • Токены: NN prompt + NN completion
      • Вызовов: N
      • Модели: claude-haiku-4-5 (N), gpt-4 (N)
    ```
    No `Расход` / `Потрачено впустую` / `$` line.
- **Deep link** computed as `<settings.web_ui_base_url>/admin/usage?project_id=<id>&window=1d`. The base URL is a `Settings` field (likely already exists; check `Settings.web_ui_base_url`).
- **Text-only** — no chart images. Telegram MarkdownV2 or HTML safe (escape operator-supplied content per the existing escape helpers; project names and model names are escaped before insertion).
- **Empty-state**: if all three trackers return zero rows for today, reply "Сегодня данных пока нет." (Russian).
- **Degraded state**: if the api returns 503, reply "Данные использования временно недоступны." (Russian).
- **Cost-byte-cleanness check** — the operator-output formatter asserts (in tests) that the formatted output contains zero `$` characters AND zero `Расход` / `Потрачено впустую` substrings.

### Out of Scope
- Web UI dashboard (14.06 — but `/usage` links to it).
- API endpoints (14.07 — `/usage` consumes them).
- Alerting (14.09).
- Multi-project rollup in `/usage` output — `/usage` is one-project-at-a-time (admin can re-run with different `<project_name>`).
- Historical-day queries via `/usage` — only "today" in admin/operator local time (consistent with the dashboard's default 1d view).
- Export to file / PDF — future epic.

## Implementation Notes
- **Start-of-message anchor + Epic 10 gating** — match the existing pattern (`_SLASH_RE` in `kb_intent.py`, `_handle_admin_hitl_command`). Operator-gating uses the Epic 10 registry; non-registered → `_skip(reason='unauthorized_usage')`, no DM.
- **`as_user` propagation** — always pass `as_user=<sender username>` so the api applies the right scope. Admins → admin scope; operators → operator scope.
- **"Today" in user's local timezone** — for the bot output, "today" means the user's local-midnight-to-now. Since the bot doesn't have JS to read browser-tz, use the project's configured timezone from `hitl_runtime_config` (the same source the calendar feature uses; falls back to UTC if unset). The summary endpoint takes UTC range params so the bot converts local-today-midnight back to UTC before calling.
- **Cost formatting** — `f"${value:.2f}"` for cost; thousands-separator if > $1k. NULL cost → "—" (em-dash) for admins.
- **Token formatting** — thousands-separator (e.g. "12,345"); show `prompt + completion` separately.
- **Russian strings** in `data/russian_usage_strings.json`:
  - `admin_template`, `operator_template`, `empty_state`, `degraded_state`, `no_project_assigned_operator`, `admin_specify_project`, etc.
  - Match the existing Russian-first-content-is-DATA convention.
- **Service-to-service auth** — the bot calls `/api/usage/summary` via `internal_service_token`. Already configured. Setting `as_user=` is the new bit per 14.07.
- **Error handling** — httpx errors → DM the degraded-state message; never propagate to a 5xx that disappears the bot. Log `usage_command_api_failed`.
- **Latency target** — `/usage` typed → DM received within ~1s. Single api call (or two for admin); no extra round-trips.
- **Telegram message length** — keep under Telegram's 4096-char limit. Truncate model-breakdown to top 5 models if longer.

## Test Plan

### Unit
- `tests/test_usage_command_routing.py`:
  - `/usage` from a registered operator on project 1 → calls `ApiClient.fetch_usage_today(project_id=1, scope='operator', as_user='operator1')`; DMs the operator-formatted reply.
  - `/usage` from an admin in project 1's chat → resolves to project 1; calls fetch with `scope='admin'`; DMs the admin-formatted reply (with cost).
  - `/usage <project_name>` from admin → resolves to that project by name lookup; same admin flow.
  - `/usage` from an admin with no project context and no argument → DMs "укажите проект: …" with top 10 projects.
  - `/usage` from a non-registered sender → no DM; logged `unauthorized_usage`.
  - `/usage` from an operator with no project assignment → DM "у вас не назначен проект".
- `tests/test_usage_formatter.py`:
  - Admin format with synthetic input → rendered string matches the template + contains "$" and "Расход".
  - Operator format with same input → rendered string contains zero "$" and zero "Расход" / "Потрачено впустую" substrings (byte-cleanness assertion).
  - Empty state → "Сегодня данных пока нет."
  - Degraded state → "Данные использования временно недоступны."
  - Multi-model: 7 models in input → output shows top 5 + "(и ещё 2)".
- `tests/test_api_client_fetch_usage_today.py`:
  - Builds the correct query params (today in project tz → UTC range).
  - Passes `as_user=<sender>` header.
  - Admin scope calls both `/summary` and `/wasted`; operator scope calls only `/summary`.
  - httpx error → returns a "degraded" sentinel; caller formats degraded reply.

### Contract
- The api endpoints are owned by 14.07; this story uses them.

### Integration
- `tests/test_usage_command_integration.py` — boot the api + bot_gateway test client; seed summary rows; invoke `/usage` as admin and as operator; assert the DMs match expected formats; assert byte-cleanness of operator output.

## Automated E2E verification
- `tests/e2e/test_e2e_epic14_usage_command.py` (`@pytest.mark.e2e @pytest.mark.epic("14") @pytest.mark.story("14-08")`):
  - Send a Telegram `/usage` via the mocked webhook as a registered operator → DM appears; body contains zero `$` characters; body contains the deep link.
  - Same as admin → DM contains formatted cost figures + Wasted-spend line.
  - `/usage <project_name>` as admin → resolves to that project.
  - `/usage` as non-registered sender → no DM (verified by mock-sender call count = 0).
  - With api unreachable → DM contains degraded message.

## Manual Verification
1. `docker compose up --build -d`; let Epic 14 ship and accumulate today's data.
2. Type `/usage` as the admin (`@ajdevy`) → confirm DM with cost data + deep link.
3. Click the deep link → opens `/admin/usage?project_id=<id>&window=1d` correctly.
4. Type `/usage` as an operator → confirm DM with NO cost figures.
5. Type `/usage` as a non-registered Telegram user → confirm no DM; bot_gateway logs show `unauthorized_usage`.

## Done Criteria
- 100% line coverage on `usage_command.py`, `usage_formatter.py`, `ApiClient.fetch_usage_today`.
- `ruff check .` passes.
- Byte-cleanness verified — operator format contains zero `$` / zero `Расход` / zero `Потрачено впустую` (asserting test).
- Russian strings live in `data/russian_usage_strings.json` (project-context convention).
- Telegram message length stays under 4096 characters even with 7+ models in the LLM breakdown.
- E2E `/usage` command green.
- Epic 10.5 + Story 14.07 merged before this story merges.

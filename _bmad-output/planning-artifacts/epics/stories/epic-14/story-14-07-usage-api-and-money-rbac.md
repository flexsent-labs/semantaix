# Story 14.07 — Usage API endpoints + money RBAC (admin vs operator scope)

## Objective
Ship the four new api endpoints powering the dashboard + `/usage` bot command: `/api/usage/summary`, `/api/usage/raw`, `/api/usage/wasted` (admin only), `/api/usage/incidents`. Enforce **money RBAC at the API layer**: operator-scope responses physically exclude `cost_usd` and any derived monetary field from the SQL projection (verified by SQL-capture test). Operator-vs-admin scope resolved via the Epic 10.5 operator/project model.

**⚠️ Gated on Epic 10.5 shipping first** — this story uses the flat-list-many-operators-per-project / one-project-per-operator model. Cannot merge until Epic 10.5 lands.

**As an** admin,
**I want** APIs that return per-project usage summaries with cost data,
**So that** the dashboard and `/usage` bot command can render accurate financials.

**As an** operator,
**I want** APIs that return per-project token + volume data scoped to my assigned project, with cost data stripped at the server,
**So that** I can monitor activity without seeing money the platform considers privileged.

PRD reference: **FR-33** (Usage API + RBAC), **NFR-9** (Usage RBAC byte-cleanness).

## Scope

### In Scope
- **`services/api/app/usage/api_router.py`** — new router mounted at `/api/usage`:
  - `GET /api/usage/summary?project_id=&from=&to=&trackers=` — returns `{rows: [UsageDailySummaryRow, ...]}` for the window.
  - `GET /api/usage/raw?project_id=&day_utc=&tracker_type=&page=&page_size=` — returns paginated raw rows for a single day; rejected with 410 if `day_utc` is older than 30 days.
  - `GET /api/usage/wasted?project_id=&from=&to=` — returns `{total_wasted_usd, breakdown: [{call_outcome, cost_usd, count}, ...]}`. **Admin only** — operator scope returns 403.
  - `GET /api/usage/incidents?project_id=&from=&to=` — returns `{incidents: [UsageIncidentRow, ...]}` (state machine from 14.09; this story ships the endpoint but returns empty list until 14.09 lands).
- **Authn**: each endpoint accepts EITHER a cookie session (browser) OR `Authorization: Bearer <internal_service_token>` + `as_user=<username>` header (service-to-service from bot_gateway). Same dual-auth pattern as `/admin/files/*`.
- **Authz / scope resolution**:
  - Resolve the caller's identity → role (`admin` or `operator`).
  - For operators, resolve their assigned project via Epic 10.5's flat-list mapping. If `project_id` in the query param does NOT equal the operator's assigned project, return **403 `not_authorized_for_project`**.
  - For admins, any `project_id` is allowed (existing admin shell scoping).
  - For operators on `/api/usage/wasted` → **403 `wasted_endpoint_admin_only`**.
- **Money RBAC enforcement** — at the SQL projection layer:
  - For admin scope: `SELECT * FROM usage_daily_summary WHERE ...` (cost columns included).
  - For operator scope: `SELECT project_id, day_utc, tracker_type, model_name, prompt_tokens_total, completion_tokens_total, NULL AS cost_usd_total, NULL AS wasted_cost_usd, call_count, in_count, out_count, hitl_created_count, hitl_assigned_count, hitl_replied_count, hitl_resolved_count FROM usage_daily_summary WHERE ...`. The `NULL AS cost_usd_total` makes the column shape consistent but byte-clean of cost.
  - **Better:** an operator-scope query OMITS the cost columns entirely from the `SELECT` list (more defensive: a bug in the row-to-JSON serializer can't accidentally surface a column that wasn't selected). The response Pydantic model has `cost_usd_total: Optional[float] = None` and `wasted_cost_usd: Optional[float] = None` — those fields ARE present in the JSON but always `null` for operators.
  - **`/api/usage/raw` for operators** — when `tracker_type='llm'`, omit `cost_usd` from the `SELECT`; the row response object has `cost_usd: None`.
  - **`/api/usage/wasted` for operators** → 403 (never returns money even with NULL).
- **Pydantic response models** in `services/api/app/usage/api_models.py`:
  - `UsageDailySummaryResponseRow`, `UsageLlmCallResponseRow`, `UsageMessageResponseRow`, `UsageHitlEventResponseRow`, `UsageIncidentResponseRow`, `WastedSpendResponse`.
  - Cost fields are `Optional[float]` and serialize to `null` for operator scope.
- **Query-counter test surface** — a test helper that captures all executed SQL strings against a wrapped connection. The story's tests assert that for operator-scope `/api/usage/summary` requests, the captured `SELECT` does NOT contain `cost_usd_total` in its column list.
- **Project-scoping enforcement** is done in the api router BEFORE the SQL query (a fast `if scope=='operator' and project_id != operator.project_id: raise HTTPException(403, ...)` short-circuit). Defense in depth: even if a future bug widens the scope check, the SQL query still filters on `WHERE project_id = ?`.
- **Service-to-service `as_user=` resolution** — the api looks up the user by username via the Epic 10.5-refactored operator/admin lookup. If `as_user=` is missing, treat as admin (matches `internal_service_token` semantics today — full trust). Operators MUST pass `as_user=<their username>` from bot_gateway.
- **Structured logging** — `usage_api_request_received` (with `endpoint`, `scope`, `project_id`); never log `cost_usd` values on operator-scope routes.
- **Test fixture for Epic 10.5** — `tests/conftest_epic_10_5.py` (or shared fixtures already in place) — fixtures `admin_user`, `operator_for_project_1`, `operator_for_project_2` that simulate the new mapping. This story requires Epic 10.5 to be merged for these fixtures to exist; if 10.5 is in flight, this story stays paused.

### Out of Scope
- The dashboard UI consuming these endpoints (14.06 owns it; this story makes the endpoints available).
- The `/usage` bot command (14.08).
- The recorder endpoint `POST /api/usage/record` (14.03 owns that — separate concern).
- Incident state-machine population (14.09 writes to `usage_incidents`; this story only reads — and returns empty until 14.09 lands).
- Project-cap admin UI (14.10 / per-project alert config).
- Multi-project admin views — Epic 14's admin scope is one project at a time (project_id in URL); a multi-project rollup is a future epic.

## Implementation Notes
- **SELECT-list construction by scope** — implement as a helper `_summary_select_columns(scope: Literal['admin','operator']) -> str` returning the column list. The operator-scope returns `"project_id, day_utc, tracker_type, model_name, prompt_tokens_total, completion_tokens_total, call_count, in_count, out_count, hitl_created_count, hitl_assigned_count, hitl_replied_count, hitl_resolved_count"`. The repository method takes the column list as a parameter, OR has two variants `query_admin` / `query_operator`. **Prefer two methods**: cleaner test surface, easier to verify SQL-capture against.
- **`query_operator(...)`** in `UsageDailySummaryRepository` — same signature as `query` minus the `scope` param, returns rows with `cost_usd_total=None` and `wasted_cost_usd=None` on every row.
- **`UsageLlmCallRepository.list_for_day_admin(...)` vs `list_for_day_operator(...)`** — same pattern.
- **403 vs 404 for cross-project access** — operator querying another project gets **403** (semantically correct — they ARE authenticated, just not authorized for this project). Avoid 404 (would leak project existence).
- **Response model byte-cleanness** — the operator-scope `/api/usage/summary` response JSON is asserted to not contain the substring `"cost_usd_total"` AT ALL (Pydantic `model_dump_json(exclude_none=False)` would still include `"cost_usd_total": null`). To truly byte-clean, use `model_dump_json(exclude_unset=True)` after setting cost fields to `None`-with-`unset`. OR define a separate `UsageDailySummaryOperatorResponseRow` Pydantic model that LACKS the cost fields entirely. **Prefer the latter** — type-safety guarantees byte-cleanness without serializer gymnastics.
- **`/api/usage/wasted` shape** — admin-only; the response has `total_wasted_usd: float, breakdown: list[{call_outcome: str, cost_usd: float, count: int}]`. Built from `UsageDailySummaryRepository.query_wasted(project_id, from, to)`.
- **`/api/usage/raw` pagination** — `page: int = 1, page_size: int = 100, max_page_size = 1000`. Returns `{rows, page, page_size, total_count, has_more}`.
- **30-day raw boundary check** — `if day_utc < today - 30 days: raise HTTPException(410, detail='raw_data_purged_30d_retention')`. Computed against UTC (the dashboard layer handles tz conversion before sending the request).
- **Tracker validation** — `tracker_type` query param must be one of `llm | messages | hitl | all`; `all` returns all three tracker rows for summary endpoint.
- **OpenAPI spec** — these endpoints appear in the generated FastAPI OpenAPI doc. Tag them as `usage`.

## Test Plan

### Unit
- `tests/test_usage_api_endpoints.py`:
  - GET `/api/usage/summary?project_id=1&from=2026-05-19&to=2026-05-26&trackers=llm` as admin → 200, response contains `cost_usd_total` non-NULL field.
  - GET as operator-for-project-1 → 200, response Pydantic model has NO `cost_usd_total` field (separate model class).
  - GET as operator-for-project-2 with `?project_id=1` → 403 `not_authorized_for_project`.
  - GET as operator on `/api/usage/wasted` → 403 `wasted_endpoint_admin_only`.
  - GET `/api/usage/raw?day_utc=2026-04-01&...` (older than 30d) → 410 `raw_data_purged_30d_retention`.
  - GET without auth → 401.
  - GET with `internal_service_token` + `as_user=admin` → admin scope.
  - GET with `internal_service_token` + `as_user=operator1` (operator-for-project-1) + `?project_id=1` → operator scope.
- `tests/test_usage_api_sql_byte_cleanness.py`:
  - Use a SQL-capture wrapper around `sqlite3.Connection.execute` (or `cursor.execute`); call operator-scope `/api/usage/summary` → assert the captured SQL string does NOT contain `cost_usd_total` in the SELECT list.
  - Same assertion for `/api/usage/raw` with `tracker_type=llm` → captured SQL does not contain `cost_usd`.
- `tests/test_usage_daily_summary_repository_scoped.py`:
  - `query_admin` returns rows with non-NULL `cost_usd_total` for cost-bearing days.
  - `query_operator` returns rows where the response model has no cost fields.
  - The two query methods produce identical row counts + identical non-cost values for the same window.
- `tests/test_usage_llm_call_repository_scoped.py`:
  - `list_for_day_admin` includes `cost_usd`; `list_for_day_operator` does not.

### Contract
- `tests/contract/test_usage_api_contract.py` — schema validation of every endpoint's request/response shape; admin vs operator response models differ as documented.

### Integration
- `tests/test_usage_api_integration.py` — boot the api with seeded data; round-trip each endpoint as admin then as operator; assert byte-cleanness across the wire (parse JSON response, assert keys).

## Automated E2E verification
- `tests/e2e/test_e2e_epic14_money_rbac.py` (`@pytest.mark.e2e @pytest.mark.epic("14") @pytest.mark.story("14-07")`):
  - Boot full stack (api + scheduler with seeded summary rows); login flows as admin and as operator (via Epic 10.5 fixtures).
  - Admin call to `/api/usage/summary` → JSON body contains numeric `cost_usd_total` values.
  - Operator call to same endpoint → JSON body contains zero matches for `"cost_usd"` substring; assert zero matches for `"wasted_cost_usd"`; assert response is well-formed JSON with the expected non-cost fields.
  - Operator call to `/api/usage/wasted` → 403.
  - Operator call to other project's summary → 403.
- `tests/e2e/test_e2e_epic14_sql_capture.py` (`@pytest.mark.e2e @pytest.mark.epic("14") @pytest.mark.story("14-07")`):
  - With the SQL-capture wrapper enabled, run an operator-scope summary request → assert the captured SQL projection list excludes cost columns.

## Manual Verification
1. `docker compose up --build -d`; login to `/admin/auth` as admin → `curl /api/usage/summary?project_id=1&from=2026-05-19&to=2026-05-26&trackers=all -H "Cookie: ..."` returns JSON with cost fields.
2. Login as an operator (via the Epic 10.5 flow) → same `curl` returns JSON with NO cost keys.
3. Operator's `curl` against another project → 403.
4. Operator's `curl /api/usage/wasted` → 403.

## Done Criteria
- 100% line coverage on `services/api/app/usage/api_router.py`, `api_models.py`, the new repo methods (`query_admin`, `query_operator`, `list_for_day_admin`, `list_for_day_operator`, `query_wasted`).
- `ruff check .` passes.
- SQL-capture test proves operator-scope SELECT does NOT include cost columns.
- Pydantic operator-response models have NO cost fields (verified by `model_fields` inspection).
- E2E money-RBAC + SQL-capture tests green.
- Epic 10.5 fixtures available (story merges only after 10.5 is in main).

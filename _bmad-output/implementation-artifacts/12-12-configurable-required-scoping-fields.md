# Story 12.12: Configurable / optional scoping fields per project

Status: ready-for-dev

## Story

As an **operator configuring the sales bot for my business**,
I want to choose **which scoping fields are required** (and drop tour-specific ones like `drivers` / `difficulty` for a plain rental),
so that **the funnel asks only what's relevant and doesn't force irrelevant questions**.

**Problem:** the five scoping fields (`dates`, `headcount`, `vehicle_count`, `difficulty`, `drivers`) are hardcoded as mandatory in `Intent.is_complete()` and the scoping prompt. They come from the original guided-**tour** design; a plain buggy **rental** shouldn't have to ask "difficulty" or "how many drivers". Today every project asks all five.

## Acceptance Criteria

1. **Configurable required set.** Given a per-project config of required scoping fields, when the funnel runs, then `missing_fields` / completeness are computed against the **configured** set (not the hardcoded five), and the scoping prompt only asks for configured-missing fields.
2. **Sensible default + back-compat.** With no config set, the default required set is the current five (no behavior change for existing projects). The default is documented and overridable.
3. **Optional fields are never asked.** A field omitted from the required set is never asked and never blocks completion; if the customer volunteers it, it's still captured (extraction is unchanged — only *required-ness* changes).
4. **Operator-settable.** The required set is settable through the existing runtime-config surface (`hitl_runtime_config`) and/or `Settings`, consistent with how country/timezone/ack are configured — no code change to retune a project.
5. **Gates green.** `ruff` clean; full suite 100% coverage; tests for a reduced required set (e.g. drop `drivers`+`difficulty`), the default-five back-compat path, and a volunteered optional field still captured.

## Tasks / Subtasks

- [ ] **Move completeness to a configured set** (AC: 1, 2) — `Intent.is_complete()` / `missing_fields()` are hardcoded to `_FIELD_NAMES`. Add a required-fields parameter (e.g. `missing_fields(required: tuple[str,...])`) or compute completeness in the answerer against a configured set, keeping `Intent` a pure data holder. Validate configured names against `_FIELD_NAMES`.
- [ ] **Config plumbing** (AC: 4) — read required fields from runtime config (`hitl_ticket_repository.get_runtime_config("scoping_required_fields")`, comma-list) falling back to a `Settings.scoping_required_fields` default (the five). Mirror `_effective_*` helpers in `services/api/app/main.py`.
- [ ] **Scoping prompt uses the configured set** (AC: 1, 3) — `_build_scoping_prompt` lists only configured-required fields in `{missing_fields}`, so the LLM only asks those. Don't ask for non-required fields.
- [ ] **Thread the set into the answerer** (AC: 1) — `SalesPersonaAnswerer` gets the required-fields (injected or resolved per turn from config), used by `_handle_scoping` completeness and `_complete_booking`.
- [ ] **Tests** — reduced set completes after fewer questions; default five unchanged; volunteered optional field captured; invalid config name rejected/ignored. 100% coverage.

## Dev Notes

- **Files:** `services/api/app/sales/intent.py` (parameterize completeness, keep immutable), `services/api/app/sales/sales_persona_answerer.py` (`_handle_scoping`, `_build_scoping_prompt`, constructor — inject/resolve required set), `services/api/app/sales/system_prompts/sales_scoping.txt` (the prompt already renders `{missing_fields}` — keep it data-driven), `platform_common/settings.py` (+ `scoping_required_fields` default), `services/api/app/main.py` (`_effective_scoping_required_fields` resolver + wiring).
- **Keep `Intent` a pure value object** — don't bake config into the dataclass; pass the required set where completeness is decided (project-context rule: immutable value objects).
- **Composes with 12.11:** 12.12 decides *which* fields are asked; 12.11 handles declines/loops for the ones that remain required. Land 12.11 first if sequencing — its loop guard is a safety net regardless of the configured set.
- **Conventions:** runtime config mirrors the existing country/timezone/ack pattern; ruff E/F/I line-100; 100% coverage.

### References

- [Source: services/api/app/sales/intent.py#_FIELD_NAMES / is_complete]
- [Source: services/api/app/sales/system_prompts/sales_scoping.txt — `{missing_fields}` rendering]
- [Source: services/api/app/main.py#_effective_* runtime-config helpers]

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (Claude Code)

### Completion Notes List

- `Intent.missing_fields`/`is_complete` now take an optional `required` tuple (default `None` = legacy all-five). `_build_scoping_prompt`/`_format_missing_fields` take `required` so the LLM only asks configured-missing fields.
- `SalesPersonaAnswerer` gains `scoping_required_fields_getter` (injected); `_required_fields()` resolves it per turn (falls back to all five when unset or empty). Threaded through `_handle_scoping` (completeness + the 12.11 decline branch), `_extract_and_merge` (prompt), and `_handle_awaiting_time`.
- `Settings.scoping_required_fields` default `"dates,headcount,vehicle_count"` (drops tour-only difficulty/drivers); `_effective_scoping_required_fields()` parses runtime config / settings, drops unknown names, enforces canonical order, falls back to all five.
- Back-compat: tests that build the answerer without the getter keep all-five behavior, so no existing funnel test changed. ruff clean; full suite 100%.

### File List

- `services/api/app/sales/intent.py` (parameterized completeness)
- `services/api/app/sales/sales_persona_answerer.py` (required threading + getter)
- `services/api/app/main.py` (`_effective_scoping_required_fields` + wiring)
- `platform_common/settings.py`, `.env.example` (`scoping_required_fields`)
- `tests/test_sales_scoping_required_fields.py` (new)

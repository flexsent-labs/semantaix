# Story 12.41: Valid LLM model + startup/health model-availability guard (round-7 root cause, P0)

Status: review

## Story

As an **operator running the sales bot**,
I want the bot to **use a model OpenRouter actually serves, and to fail loudly if that ever stops being true**,
so that **a retired model slug never silently turns every reply into "Это не ко мне" / a stall.**

**Problem (round-7, 2 Jun 2026 — live):** `#37 "Здравствуйте!" → "Это не ко мне"`, `#36` clean booking → no reply. Bot effectively non-functional.

**Root cause (CONFIRMED, High — live logs + OpenRouter /models):** the persona calls `complete_json(system, user)` with no model → defaults to `OpenRouterClient.grounding_model` = `settings.openrouter_grounding_model` = **`google/gemini-2.0-flash-lite-001`**, which OpenRouter **retired** (absent from its live 342-model list) → **`POST /chat/completions` → 404** → `sales_llm_transport_error` → thin-gate `_skip` → greeting (not in-scope) hits ScopeGuard's decline ("Это не ко мне"), bookings escalate/stall. It worked in round 5 (1 Jun) and broke in round 6–7 — OpenRouter deprecated the slug between those dates. The round-6/7 "D10/D12 defects" were **symptoms of this dead model**, not new code bugs (12.36–12.40 handle the degraded mode correctly but can't make the bot functional while every call 404s).

## Acceptance Criteria

1. **Config fix:** `openrouter_grounding_model` → a live slug (`google/gemini-2.5-flash-lite`) in `settings.py` default + `.env.example`. The live `.env` doesn't override it, so the default is what runs.
2. **Startup guard:** at api boot, validate the configured models against OpenRouter's live model list; log `llm_model_unavailable` (ERROR) per missing model + `llm_models_unavailable_at_startup`. Skips when the key is the unconfigured placeholder (unit tests never reach the network — mirrors `sync_telegram_identity_on_startup`).
3. **Health endpoint:** `GET /health/model` → 200 `{status: ok}` when all configured models are served; **503** `{status: degraded, unavailable: [...]}` when one is gone — so monitoring / a post-deploy probe catches a future deprecation before customers do.
4. **Conservative:** an unconfigured key or an unreachable model list is "can't verify", NEVER a false "unavailable" alarm (`llm_model_check_failed` WARNING).
5. Gates green; 100% coverage.

## Why a guard (not just the slug fix)

This exact failure — a silently-retired model — cost **multiple live-QA rounds** to trace (rounds 6 and 7 were both this). Model deprecation will recur. The guard turns a silent customer-facing outage into a loud startup log + a 503 health signal.

## Tasks / Subtasks

- [x] `platform_common/settings.py` + `.env.example`: grounding model → `google/gemini-2.5-flash-lite`.
- [x] `OpenRouterClient.fetch_available_model_ids()` (GET /models) + `_is_configured()`.
- [x] `services/api/app/llm_model_health.py#find_unavailable_models` — validate models, log loudly, never false-alarm.
- [x] api startup event `validate_llm_models_on_startup` (gated on configured key) + `GET /health/model` (503 on degraded).
- [x] Tests (TDD): client method (+requires-key), helper (present / missing+logged / transport-error / unconfigured-skip / empty), endpoint (200 / 503), startup (missing-logs / ok-logs / unconfigured-skips-network).

## Dev Notes

- **Test safety:** the startup event makes a network call only when the key is configured; the test suite's `openrouter_client` has `api_key=None` (default), so it skips — the ~hundreds of `TestClient(api_app)` tests don't hit the network. The configured path is covered by calling `validate_llm_models_on_startup()` directly with mocks.
- **Persona uses grounding_model** (`complete_json` default), so fixing `openrouter_grounding_model` restores both the persona and the grounded-RAG verifier. (Whether the persona *should* use the generative `openrouter_model` is a separate design question; out of scope.)
- **Files:** `platform_common/settings.py`, `.env.example`, `services/api/app/openrouter_client.py`, `services/api/app/llm_model_health.py` (new), `services/api/app/main.py`.

## References

- Investigation: `_bmad-output/implementation-artifacts/investigations/booking-dialog-round6-blockers-investigation.md` (Follow-up 2026-06-02 round 7).
- Round-7 QA #36/#37; live evidence: `docker logs semantaix-api-1` → `POST …/chat/completions "404"` + `sales_llm_transport_error`; OpenRouter `/models` (slug absent).

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–5, round-7 root cause).** Dead model slug replaced with `google/gemini-2.5-flash-lite`; added a startup guard + `/health/model` (503-on-degraded) so a future deprecation is caught loudly instead of via customer-facing breakage.
- **Root cause was config, not code:** OpenRouter retired `gemini-2.0-flash-lite-001` between rounds 5 and 6; every persona call 404'd. The 12.36–12.40 fixes correctly handle the degraded mode but couldn't restore function while the model 404s.
- **TDD; 100% coverage** on the new module + client methods; full suite green.

### File List

- `platform_common/settings.py` (modified — grounding model default)
- `.env.example` (modified)
- `services/api/app/openrouter_client.py` (modified — `_is_configured`, `fetch_available_model_ids`)
- `services/api/app/llm_model_health.py` (new)
- `services/api/app/main.py` (modified — startup guard + `/health/model`)
- `tests/test_openrouter_client.py`, `tests/test_llm_model_health.py` (new), `tests/test_api_health_model.py` (new)
- `_bmad-output/implementation-artifacts/12-41-valid-llm-model-and-guard.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)

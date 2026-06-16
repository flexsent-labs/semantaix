"""Russian-first inbound pipeline e2e: deterministic -> grounded -> scope-guard -> HITL."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import services.api.app.main as api_main
from services.api.app.answerers import AnswerResult
from services.api.app.answerers.scope_guard import RESPONSE_MODE_SCOPE_DECLINE
from services.api.app.answerers.weather_client import WeatherSummary
from services.api.app.main import (
    answer_pipeline,
    answer_trace_repository,
    calendar_clarify_state_repository,
    calendar_settings_repository,
    hitl_ticket_repository,
    incident_repository,
    openrouter_client,
    rag_repository,
    settings,
    telegram_bot_sender,
    weather_client,
)
from services.api.app.main import app as api_app
from services.api.app.openrouter_client import GroundingVerdict, LlmUsageCapture
from services.bot_gateway.app.main import (
    api_client as bot_api_client,
)
from services.bot_gateway.app.main import app as bot_app
from services.bot_gateway.app.main import (
    hitl_ticket_repository as bot_hitl_repo,
)

pytestmark = [pytest.mark.e2e, pytest.mark.epic("pipeline")]


def _wire(tmp_path, monkeypatch):
    hitl_path = str(tmp_path / "hitl.sqlite3")
    hitl_ticket_repository.db_path = hitl_path
    bot_hitl_repo.db_path = hitl_path
    incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    rag_repository.db_path = str(tmp_path / "rag.sqlite3")
    answer_trace_repository.db_path = str(tmp_path / "answer_traces.sqlite3")
    # Isolate the calendar subsystem too. CalendarAvailabilityAnswerer runs
    # BEFORE GroundedRagAnswerer and reads the shared calendar DB; a leftover
    # calendar-enabled project there (e.g. from a live demo or signoff run)
    # would make it own these scheduling questions and escalate to HITL instead
    # of letting them fall through to RAG. A fresh tmp DB reads "calendar off"
    # for every project, so the answerer is the intended no-op skip. We mutate
    # db_path on the shared singletons (the pipeline's answerer holds these very
    # instances) and re-init the schema at the tmp path; monkeypatch restores
    # the real path at teardown so we never leak the tmp DB into later tests.
    calendar_db = str(tmp_path / "calendar.sqlite3")
    monkeypatch.setattr(calendar_settings_repository, "db_path", calendar_db)
    calendar_settings_repository.init_schema()
    monkeypatch.setattr(calendar_clarify_state_repository, "db_path", calendar_db)
    calendar_clarify_state_repository.init_schema()
    # Isolate the bot_gateway persistence DB too: the gateway now
    # short-circuits on duplicate source_message_id, so leaking rows from
    # earlier test runs would make the first webhook in this test appear
    # as a duplicate. We monkeypatch the attribute on the cached settings
    # singleton rather than setenv+cache_clear, because clearing the
    # lru_cache breaks downstream tests that monkeypatch get_settings()
    # (which would return a different instance from the api/main module's
    # module-level ``settings`` binding).
    monkeypatch.setattr(
        settings, "persistence_db_path", str(tmp_path / "persistence.sqlite3")
    )
    monkeypatch.setattr(api_main, "_effective_hitl_operator_username", lambda: "@operator")
    async def _sales_skip(*, question, ctx):
        return AnswerResult(handled=False)

    monkeypatch.setattr(api_main.sales_persona_answerer, "try_answer", _sales_skip)


def _post_inbound(client, **kwargs):
    return client.post("/conversations/inbound", json=kwargs).json()


def test_e2e_bare_factual_question_declines(tmp_path, monkeypatch):
    # Off-topic questions (no grounding, no sales intent) now get a polite
    # scope-decline from the scope guard — no HITL ticket created.
    _wire(tmp_path, monkeypatch)
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    monkeypatch.setattr(api_main, "_should_send_interim", lambda text, chat_id: False)
    client = TestClient(api_app)

    body = _post_inbound(
        client,
        text="Какое сегодня число?",
        chat_id=9001,
        customer_username="@customer",
        trace_id="t-det-date",
    )
    assert body["delivered"] is True
    assert body["escalated"] is False
    assert body["response_mode"] == RESPONSE_MODE_SCOPE_DECLINE

    tickets = client.get("/hitl/tickets").json()["items"]
    assert tickets == []
    send_mock.assert_awaited_once()


def test_e2e_scheduling_question_enriches_grounded_answer(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    hitl_ticket_repository.set_runtime_config(
        key="rag_grounding_score_threshold", value="0.2", updated_by="@admin"
    )
    rag_repository.ingest(
        source_id="kb-delivery",
        text="Доставка заказов выполняется в течение одного рабочего дня",
    )
    monkeypatch.setattr(
        weather_client,
        "fetch",
        AsyncMock(
            return_value=WeatherSummary(
                location_name="Moscow",
                temperature_c=15.0,
                condition_ru="переменная облачность",
                condition_en="partly cloudy",
            )
        ),
    )
    answer_mock = AsyncMock(return_value=(
        "Доставим ваш заказ завтра.",
        LlmUsageCapture(
            model_name="gpt-4o", prompt_tokens=50, completion_tokens=25,
            cost_usd=0.001, created_at="2026-06-11T00:00:00Z",
        ),
    ))
    monkeypatch.setattr(openrouter_client, "answer_grounded", answer_mock)
    monkeypatch.setattr(
        openrouter_client,
        "verify_grounding",
        AsyncMock(
            return_value=(
                GroundingVerdict(label="GROUNDED", reason="matches snippet"),
                LlmUsageCapture(
                    model_name="gpt-4o", prompt_tokens=20, completion_tokens=10,
                    cost_usd=0.0005, created_at="2026-06-11T00:00:00Z",
                ),
            )
        ),
    )

    client = TestClient(api_app)
    body = _post_inbound(
        client,
        text="можете доставить заказ завтра в Москве?",
        chat_id=9001,
        trace_id="t-sched",
    )
    assert body["response_mode"] == "grounded_rag"
    # Scheduling context (with datetime/holiday/weather facts) reached the LLM.
    scheduling_context = answer_mock.await_args.kwargs["scheduling_context"]
    assert scheduling_context is not None
    assert "Справочный контекст для планирования" in scheduling_context
    assert "Погода сейчас (Moscow)" in scheduling_context


def test_e2e_grounded_rag_russian_answer(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    hitl_ticket_repository.set_runtime_config(
        key="rag_grounding_score_threshold", value="0.2", updated_by="@admin"
    )
    rag_repository.ingest(
        source_id="kb-refunds",
        text="Возврат денег занимает пять рабочих дней",
    )
    monkeypatch.setattr(
        openrouter_client,
        "answer_grounded",
        AsyncMock(return_value=(
            "Возврат денег занимает пять рабочих дней.",
            LlmUsageCapture(
                model_name="gpt-4o", prompt_tokens=50, completion_tokens=25,
                cost_usd=0.001, created_at="2026-06-11T00:00:00Z",
            ),
        )),
    )
    monkeypatch.setattr(
        openrouter_client,
        "verify_grounding",
        AsyncMock(
            return_value=(
                GroundingVerdict(label="GROUNDED", reason="matches snippet"),
                LlmUsageCapture(
                    model_name="gpt-4o", prompt_tokens=20, completion_tokens=10,
                    cost_usd=0.0005, created_at="2026-06-11T00:00:00Z",
                ),
            )
        ),
    )

    client = TestClient(api_app)
    body = _post_inbound(
        client,
        text="когда придёт мой возврат?",
        chat_id=9001,
        trace_id="t-grounded",
    )
    assert body["response_mode"] == "grounded_rag"
    assert "пять рабочих дней" in body["answer_text"]


def test_e2e_full_hitl_journey_via_bot_gateway(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    # Force HITL escalation by stubbing the pipeline to return handled=False.
    # This test exercises the ticket-creation and operator-reply mechanism,
    # not the pipeline answerer logic (scope guard would otherwise fire).
    monkeypatch.setattr(
        answer_pipeline,
        "run",
        AsyncMock(return_value=AnswerResult(handled=False)),
    )
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))

    async def _find_operator(*, username: str):
        if username == "@operator":
            return {"username": "@operator", "chat_id": 1, "project_id": 1, "is_active": True}
        return None

    monkeypatch.setattr(bot_api_client, "find_operator_by_username", _find_operator)

    api_client = TestClient(api_app)

    # 1. Customer sends a free-form question through bot_gateway webhook
    bot_client = TestClient(bot_app)
    customer_payload = {
        "update_id": 8001,
        "message": {
            "message_id": 1,
            "from": {"id": 9001, "username": "customer"},
            "chat": {"id": 9001, "type": "private"},
            "text": "Когда придёт мой возврат?",
        },
    }
    # The bot_gateway will try to forward to api over httpx — short-circuit
    # by patching api_client.forward_inbound to call the api app directly.
    async def _forward(
        *, text, chat_id, customer_username, trace_id, timeout_seconds=None
    ):
        return api_client.post(
            "/conversations/inbound",
            json={
                "text": text,
                "chat_id": chat_id,
                "customer_username": customer_username,
                "trace_id": trace_id,
            },
        ).json()

    monkeypatch.setattr(bot_api_client, "forward_inbound", _forward)
    webhook = bot_client.post("/telegram/webhook", json=customer_payload)
    assert webhook.status_code == 200
    assert webhook.json()["status"] == "accepted"

    tickets = api_client.get("/hitl/tickets").json()["items"]
    assert len(tickets) == 1
    ticket_id = tickets[0]["id"]
    assert tickets[0]["target_chat_id"] == 9001
    assert tickets[0]["operator_username"] == "@operator"
    assert tickets[0]["status"] == "assigned"

    # 2. Operator sends a Telegram reply quoting the ticket DM. Bot_gateway
    # routes via api_client.deliver_operator_reply -> /hitl/tickets/{id}/reply.
    async def _deliver(*, ticket_id, operator_username, reply_text):
        return api_client.post(
            f"/hitl/tickets/{ticket_id}/reply",
            json={
                "operator_username": operator_username,
                "reply_text": reply_text,
            },
        ).json()

    monkeypatch.setattr(bot_api_client, "deliver_operator_reply", _deliver)

    operator_payload = {
        "update_id": 8002,
        "message": {
            "message_id": 2,
            "from": {"id": 1, "username": "operator"},
            "chat": {"id": 1, "type": "private"},
            "text": "В течение 5 рабочих дней.",
            "reply_to_message": {
                "text": f"HITL ticket #{ticket_id} | from @customer | Когда возврат?"
            },
        },
    }
    op_resp = bot_client.post("/telegram/webhook", json=operator_payload)
    assert op_resp.status_code == 200
    assert op_resp.json()["status"] == "operator_reply_delivered"

    # 3. Ticket auto-resolved
    final = hitl_ticket_repository.get(ticket_id)
    assert final.status == "resolved"
    assert final.resolved_at is not None


def test_e2e_english_scheduling_question_grounded(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    hitl_ticket_repository.set_runtime_config(
        key="rag_grounding_score_threshold", value="0.2", updated_by="@admin"
    )
    rag_repository.ingest(
        source_id="kb-delivery-en",
        text="Возврат денег занимает пять рабочих дней",
    )
    answer_mock = AsyncMock(return_value=(
        "Мы доставим завтра.",
        LlmUsageCapture(
            model_name="gpt-4o", prompt_tokens=50, completion_tokens=25,
            cost_usd=0.001, created_at="2026-06-11T00:00:00Z",
        ),
    ))
    monkeypatch.setattr(openrouter_client, "answer_grounded", answer_mock)
    monkeypatch.setattr(
        openrouter_client,
        "verify_grounding",
        AsyncMock(return_value=(
            GroundingVerdict(label="GROUNDED", reason="ok"),
            LlmUsageCapture(
                model_name="gpt-4o", prompt_tokens=20, completion_tokens=10,
                cost_usd=0.0005, created_at="2026-06-11T00:00:00Z",
            ),
        )),
    )
    client = TestClient(api_app)
    body = _post_inbound(
        client,
        text="можете доставить мой возврат?",
        chat_id=9001,
        trace_id="t-sched-en",
    )
    assert body["response_mode"] == "grounded_rag"
    assert answer_mock.await_args.kwargs["scheduling_context"] is not None


def test_e2e_slang_rag_via_lemma_overlap(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    hitl_ticket_repository.set_runtime_config(
        key="rag_grounding_score_threshold", value="0.2", updated_by="@admin"
    )
    rag_repository.ingest(
        source_id="kb-money",
        text="Возврат денег занимает пять рабочих дней",
    )
    monkeypatch.setattr(
        openrouter_client,
        "answer_grounded",
        AsyncMock(return_value=(
            "Деньги вернутся за пять рабочих дней.",
            LlmUsageCapture(
                model_name="gpt-4o", prompt_tokens=50, completion_tokens=25,
                cost_usd=0.001, created_at="2026-06-11T00:00:00Z",
            ),
        )),
    )
    monkeypatch.setattr(
        openrouter_client,
        "verify_grounding",
        AsyncMock(return_value=(
            GroundingVerdict(label="GROUNDED", reason="ok"),
            LlmUsageCapture(
                model_name="gpt-4o", prompt_tokens=20, completion_tokens=10,
                cost_usd=0.0005, created_at="2026-06-11T00:00:00Z",
            ),
        )),
    )
    client = TestClient(api_app)
    # "бабло" -> "деньги" -> lemma overlap with "денег" chunk
    body = _post_inbound(client, text="когда придёт бабло?", chat_id=9001)
    assert body["response_mode"] == "grounded_rag"


def test_e2e_profanity_in_llm_output_declines(tmp_path, monkeypatch):
    # When guardrails block profane LLM output, GroundedRagAnswerer returns
    # handled=False and the scope guard picks it up with a polite decline.
    _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    monkeypatch.setattr(api_main, "_should_send_interim", lambda text, chat_id: False)
    hitl_ticket_repository.set_runtime_config(
        key="rag_grounding_score_threshold", value="0.2", updated_by="@admin"
    )
    rag_repository.ingest(
        source_id="kb-x",
        text="Возврат денег занимает пять рабочих дней",
    )
    monkeypatch.setattr(
        openrouter_client,
        "answer_grounded",
        AsyncMock(return_value=(
            "Полный пиздец с возвратами в эти дни.",
            LlmUsageCapture(
                model_name="gpt-4o", prompt_tokens=50, completion_tokens=25,
                cost_usd=0.001, created_at="2026-06-11T00:00:00Z",
            ),
        )),
    )
    monkeypatch.setattr(
        openrouter_client,
        "verify_grounding",
        AsyncMock(return_value=(
            GroundingVerdict(label="GROUNDED", reason="ok"),
            LlmUsageCapture(
                model_name="gpt-4o", prompt_tokens=20, completion_tokens=10,
                cost_usd=0.0005, created_at="2026-06-11T00:00:00Z",
            ),
        )),
    )
    client = TestClient(api_app)
    body = _post_inbound(
        client, text="когда придёт возврат?", chat_id=9001, trace_id="t-profane"
    )
    assert body["delivered"] is True
    assert body["escalated"] is False
    assert body["response_mode"] == RESPONSE_MODE_SCOPE_DECLINE


def test_e2e_english_factual_question_declines(tmp_path, monkeypatch):
    # English off-topic question with no grounding → scope guard polite decline.
    _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    monkeypatch.setattr(api_main, "_should_send_interim", lambda text, chat_id: False)
    client = TestClient(api_app)
    body = _post_inbound(client, text="what is the date?", chat_id=9001)
    assert body["delivered"] is True
    assert body["escalated"] is False
    assert body["response_mode"] == RESPONSE_MODE_SCOPE_DECLINE

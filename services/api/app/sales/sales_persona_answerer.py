"""SalesPersonaAnswerer — greeting, scoping, asides, proposing, closing.

Activation gate (always-on, cheap, first):
  1. Existing non-dormant state → resume in that stage.
  2. No state + sales intent → enter the greeting stage.
  3. Otherwise → `_skip("not_sales_intent")` and fall through.

Stages implemented:
  * `new` → greeting → transition to `scoping` (Story 12.03).
  * `scoping` → ask the next missing intent field (Story 12.03).
  * `pricing` / `awaiting_operator_price` → KB-first price quote with
    escalate-if-unknown (Story 12.04). On a price ask the answerer hits
    the existing RAG knowledge base, quotes a verbatim price token when
    one exists, and otherwise escalates to HITL with
    ``reason='price_unknown'`` so the operator's reply feeds Epic-06's
    knowledge extractor — the next identical ask hits the KB.
  * `proposing` → date proposer (Story 12.07): renders a verified slot,
    handles acceptance (→ closing), and escalates on calendar errors.
  * `closing` → handoff line + HITL ticket with
    ``reason='sales_closing_handoff'`` (Story 12.07).
  * Mid-funnel asides (Story 12.06) handled inline in any active stage.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from services.api.app.answerers import AnswerContext, AnswerResult
from services.api.app.rag import RagChunk
from services.api.app.sales.acceptance import is_acceptance
from services.api.app.sales.client_materials_repository import ClientMaterial
from services.api.app.sales.date_parser import parse_russian_date_span
from services.api.app.sales.date_proposer import (
    NO_PROPOSAL_AMBIGUOUS_SERVICE,
    NO_PROPOSAL_CALENDAR_NOT_ENABLED,
    NO_PROPOSAL_NO_DATE_HINT,
    NO_PROPOSAL_PROVIDER_ERROR,
    NoProposal,
    Proposal,
)
from services.api.app.sales.intent import Intent, intent_merge
from services.api.app.sales.price_lookup import (
    PriceFound,
    PriceMissing,
    extract_price_tokens,
)
from services.api.app.sales.russian_sales_intent import is_sales_intent
from services.api.app.sales.turn_intent import classify_turn

logger = logging.getLogger(__name__)

FOLLOWUP_DELAY = timedelta(hours=24)

NAME = "sales_persona"
STAGE_NEW = "new"
STAGE_SCOPING = "scoping"
STAGE_PITCHING = "pitching"
STAGE_PRICING = "pricing"
STAGE_AWAITING_OPERATOR_PRICE = "awaiting_operator_price"
STAGE_PROPOSING = "proposing"
STAGE_CLOSING = "closing"
STAGE_DORMANT = "dormant"

RESPONSE_MODE_SALES_ESCALATION = "sales_escalation"

HITL_REASON_CALENDAR_DISABLED = "date_calendar_disabled"
HITL_REASON_PROPOSAL_DRIFT = "sales_proposal_drift"
HITL_REASON_PROPOSAL_FAILED = "sales_proposal_failed"
HITL_REASON_CLOSING_HANDOFF = "sales_closing_handoff"
HITL_REASON_PRICE_UNKNOWN = "price_unknown"
HITL_REASON_EMPTY_CATALOG = "catalog_empty"

# Customer-facing Russian copy for the proposing / closing branches. Kept
# inline as named constants — short, fixed strings, no LLM in the loop for
# the fallback cases.
PROPOSAL_FALLBACK_CALENDAR_DISABLED = "Дату подтвержу у коллег."
PROPOSAL_FALLBACK_UNAVAILABLE = "Уточню свободные даты и сразу сообщу."
PROPOSAL_AMBIGUOUS_SERVICE_CLARIFIER = "На каком туре остановимся?"
CLOSING_HANDOFF_LINE = "Передам коллегам для подтверждения, на связи."
PRICING_MISS_FALLBACK = "Уточню у коллег и сразу сообщу"
EMPTY_CATALOG_ESCALATION_LINE = "Услуг пока нет. Уточню у коллег и сразу сообщу."
# Story 12.05 — appended to the textual reply when a media dispatch failed
# mid-turn so the customer never sees a silent bot.
MATERIAL_DISPATCH_FALLBACK_LINE = (
    "Видео/фото пришлю чуть позже — уточню у коллег."
)
EQUIPMENT_ACK_LINE = "Снаряжение подготовим, расскажу подробнее на месте."

# Story 12.05 — equipment-question lemma triggers. Lowercase lemma roots
# matched against ``RussianNormalizer.lemmas(question)``; any overlap fires
# the equipment_gallery media moment.
_EQUIPMENT_LEMMAS: frozenset[str] = frozenset(
    {
        "снаряжение",
        "экипировка",
        "шлем",
        "одежда",
        "одеваться",
        "обуть",
        "одеть",
        "обувь",
    }
)
_EQUIPMENT_PHRASES: tuple[tuple[str, ...], ...] = (
    ("что", "нужно"),
)

_HANDLED_STAGES: frozenset[str] = frozenset(
    {
        STAGE_NEW,
        STAGE_SCOPING,
        STAGE_PRICING,
        STAGE_AWAITING_OPERATOR_PRICE,
        STAGE_PROPOSING,
        STAGE_CLOSING,
    }
)
_DEFERRED_STAGES: frozenset[str] = frozenset({STAGE_PITCHING})
# Stages where mid-funnel asides (catalog / concept / price) are intercepted
# before the stage handler. Greeting is excluded: a brand-new chat hits the
# greeting branch first and the sales-intent gate already covers catalog
# phrases.
_ASIDE_INTERCEPT_STAGES: frozenset[str] = (
    frozenset({STAGE_SCOPING}) | _DEFERRED_STAGES
)

# Russian month names in the genitive case — used to format proposal
# dates ("1 мая", "15 июня"). Indexed by ``date.month``.
_MONTHS_GENITIVE: dict[int, str] = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}

_PROMPTS_DIR = Path(__file__).resolve().parent / "system_prompts"
_GREETING_PROMPT_PATH = _PROMPTS_DIR / "sales_greeting.txt"
_SCOPING_PROMPT_PATH = _PROMPTS_DIR / "sales_scoping.txt"
_CATALOG_PROMPT_PATH = _PROMPTS_DIR / "sales_catalog.txt"
_CONCEPT_RAG_PROMPT_PATH = _PROMPTS_DIR / "sales_concept_rag.txt"
_PROPOSAL_PROMPT_PATH = _PROMPTS_DIR / "sales_proposal.txt"
_PRICING_HIT_PROMPT_PATH = _PROMPTS_DIR / "sales_pricing_hit.txt"


def _read_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


_GREETING_PROMPT_TEMPLATE = _read_prompt(_GREETING_PROMPT_PATH)
_SCOPING_PROMPT_TEMPLATE = _read_prompt(_SCOPING_PROMPT_PATH)
_CATALOG_PROMPT_TEMPLATE = _read_prompt(_CATALOG_PROMPT_PATH)
_CONCEPT_RAG_PROMPT_TEMPLATE = _read_prompt(_CONCEPT_RAG_PROMPT_PATH)
_PROPOSAL_PROMPT_TEMPLATE = _read_prompt(_PROPOSAL_PROMPT_PATH)
_PRICING_HIT_PROMPT_TEMPLATE = _read_prompt(_PRICING_HIT_PROMPT_PATH)


def _format_proposal_date(date_iso: str) -> str:
    """Render an ISO date as ``"<day> <month_genitive>"`` for proposals."""
    parsed = date.fromisoformat(date_iso)
    return f"{parsed.day} {_MONTHS_GENITIVE[parsed.month]}"


class LlmSchemaViolation(Exception):
    """Raised when the LLM's JSON-out response fails the structured schema."""


class _Normalizer(Protocol):
    def lemmas(self, text: str) -> list[str]: ...


class _StateRepo(Protocol):
    def get(self, chat_id: int) -> dict[str, Any] | None: ...

    def upsert(self, **kwargs: Any) -> None: ...


class _ServiceRow(Protocol):
    """Minimal duck-type for a service row used by the catalog/concept asides."""

    name: str
    description: str | None


class _ServicesRepo(Protocol):
    def count_active(self, *, project_id: int) -> int: ...

    def list_for_project(
        self, *, project_id: int
    ) -> list[_ServiceRow]: ...

    def get_by_name(
        self, *, project_id: int, name: str
    ) -> _ServiceRow | None: ...


class _RagRetriever(Protocol):
    def retrieve(
        self,
        *,
        query: str,
        limit: int = 3,
        project_id: int | None = None,
    ) -> list[RagChunk]: ...


class _FollowupRepo(Protocol):
    def enqueue(
        self,
        *,
        chat_id: int,
        project_id: int,
        fire_at: datetime,
        now: datetime,
    ) -> int: ...


class _OpenRouter(Protocol):
    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
    ) -> dict[str, Any]: ...


class _DateProposer(Protocol):
    async def propose(
        self,
        *,
        project_id: int,
        intent: Intent,
        now: datetime,
    ) -> Proposal | NoProposal: ...


class _PriceLookup(Protocol):
    async def lookup(
        self,
        *,
        project_id: int | None,
        intent: Intent,
        question: str,
    ) -> PriceFound | PriceMissing: ...


class _MaterialSelector(Protocol):
    def pick(
        self,
        *,
        project_id: int,
        intent_tags: list[str],
        purpose: str,
    ) -> ClientMaterial | None: ...


MaterialDispatcher = Callable[..., Awaitable[dict[str, Any]]]


def _skip(reason: str) -> AnswerResult:
    return AnswerResult(handled=False, metadata={"skip_reason": reason})


def _validate_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Enforce the JSON-out schema. Raises `LlmSchemaViolation` on failure."""
    if not isinstance(payload, dict):
        raise LlmSchemaViolation("payload is not a dict")
    extracted = payload.get("extracted_fields", {})
    if not isinstance(extracted, dict):
        raise LlmSchemaViolation("extracted_fields is not a dict")
    next_question = payload.get("next_question")
    if not isinstance(next_question, str):
        raise LlmSchemaViolation("next_question missing or not a string")
    return extracted, next_question


def _format_known_fields(intent: Intent) -> str:
    items = []
    for name, value in intent.to_dict().items():
        if value is not None:
            items.append(f"- {name}: {value}")
    return "\n".join(items) if items else "(пока ничего)"


def _format_missing_fields(intent: Intent) -> str:
    missing = intent.missing_fields()
    return "\n".join(f"- {name}" for name in missing) if missing else "(все собраны)"


def _is_equipment_ask(text: str, *, normalizer: _Normalizer) -> bool:
    """True iff the customer is asking about gear / clothing / what to bring.

    Two-pass match: any equipment lemma in the lemmatised text wins; failing
    that, the multi-lemma "что нужно" phrase fires. Mirrors the structure of
    ``turn_intent.classify_turn`` so the gate stays cheap.
    """
    if not text or not text.strip():
        return False
    lemmas = normalizer.lemmas(text)
    if not lemmas:
        return False
    if _EQUIPMENT_LEMMAS & set(lemmas):
        return True
    lemma_set = set(lemmas)
    for phrase_tokens in _EQUIPMENT_PHRASES:
        phrase_lemmas = normalizer.lemmas(" ".join(phrase_tokens))
        if phrase_lemmas and all(token in lemma_set for token in phrase_lemmas):
            return True
    return False


def _intent_to_tags(intent: Intent) -> list[str]:
    """Derive ``intent_tags`` for the material selector from the collected
    intent. The output is intentionally small — only string-valued
    ``difficulty`` is useful as a tag today (e.g. ``"начальный"``); future
    extensions add per-service tags here without a separate signal.
    """
    out: list[str] = []
    if isinstance(intent.difficulty, str) and intent.difficulty.strip():
        out.append(intent.difficulty.strip().lower())
    return out


def _build_greeting_prompt(*, persona: str) -> str:
    return _GREETING_PROMPT_TEMPLATE.format(persona=persona)


def _build_scoping_prompt(*, persona: str, intent: Intent) -> str:
    return _SCOPING_PROMPT_TEMPLATE.format(
        persona=persona,
        known_fields=_format_known_fields(intent),
        missing_fields=_format_missing_fields(intent),
    )


class SalesPersonaAnswerer:
    name = NAME

    def __init__(
        self,
        *,
        state_repo: _StateRepo,
        services_repo: _ServicesRepo,
        openrouter: _OpenRouter,
        normalizer: _Normalizer,
        clock: Callable[[], datetime],
        bot_persona_getter: Callable[[], str],
        rag_retriever: _RagRetriever | None = None,
        grounding_threshold_getter: Callable[[], float] | None = None,
        followup_repo: _FollowupRepo | None = None,
        date_proposer: _DateProposer | None = None,
        price_lookup: _PriceLookup | None = None,
        material_selector: _MaterialSelector | None = None,
        material_dispatcher: MaterialDispatcher | None = None,
    ) -> None:
        self._state_repo = state_repo
        self._services_repo = services_repo
        self._openrouter = openrouter
        self._normalizer = normalizer
        self._clock = clock
        self._persona_getter = bot_persona_getter
        self._rag = rag_retriever
        self._grounding_threshold_getter = grounding_threshold_getter
        self._followup_repo = followup_repo
        self._date_proposer = date_proposer
        self._price_lookup = price_lookup
        self._material_selector = material_selector
        self._material_dispatcher = material_dispatcher

    async def try_answer(
        self, *, question: str, ctx: AnswerContext
    ) -> AnswerResult:
        result = await self._dispatch(question=question, ctx=ctx)
        if result.handled and ctx.chat_id is not None:
            await self._enqueue_followup(ctx=ctx)
        return result

    async def _dispatch(
        self, *, question: str, ctx: AnswerContext
    ) -> AnswerResult:
        if ctx.chat_id is None:
            return _skip("no_chat_id")

        state = await asyncio.to_thread(self._state_repo.get, ctx.chat_id)

        if state is None:
            if not is_sales_intent(question, normalizer=self._normalizer):
                return _skip("not_sales_intent")
            return await self._handle_greeting(question=question, ctx=ctx)

        current_stage = str(state.get("current_stage") or STAGE_NEW)
        if current_stage == STAGE_DORMANT:
            if not is_sales_intent(question, normalizer=self._normalizer):
                return _skip("not_sales_intent")
            return await self._handle_greeting(question=question, ctx=ctx)

        # Story 12.06 — intercept mid-funnel conversational asides BEFORE the
        # deferred-stage skip so a pitching/pricing customer can still ask
        # "что у вас есть?" or "что такое X?" without losing funnel state.
        if current_stage in _ASIDE_INTERCEPT_STAGES:
            aside = await self._maybe_handle_aside(
                question=question, ctx=ctx, state=state
            )
            if aside is not None:
                return aside

        if current_stage in _DEFERRED_STAGES:
            return _skip("stage_not_implemented_yet")

        if current_stage in (STAGE_PRICING, STAGE_AWAITING_OPERATOR_PRICE):
            return await self._handle_pricing(
                question=question, ctx=ctx, state=state
            )

        if current_stage == STAGE_SCOPING:
            return await self._handle_scoping(
                question=question, ctx=ctx, state=state
            )

        if current_stage == STAGE_PROPOSING:
            if self._date_proposer is None:
                return _skip("stage_not_implemented_yet")
            return await self._handle_proposing(
                question=question, ctx=ctx, state=state
            )

        if current_stage == STAGE_CLOSING:
            return await self._handle_closing(
                question=question, ctx=ctx, state=state
            )

        if current_stage == STAGE_NEW:
            return await self._handle_greeting(question=question, ctx=ctx)

        # Unknown / future stage value — defer to downstream answerers.
        return _skip("stage_not_implemented_yet")

    async def _enqueue_followup(self, *, ctx: AnswerContext) -> None:
        """Schedule one nudge T+24h after every successful sales turn.

        Re-enqueueing replaces any prior ``scheduled`` row for the chat —
        the queue keeps exactly one outstanding nudge per silent customer.
        """
        if self._followup_repo is None:
            return
        now = self._clock()
        try:
            await asyncio.to_thread(
                self._followup_repo.enqueue,
                chat_id=int(ctx.chat_id),  # type: ignore[arg-type]
                project_id=int(ctx.project_id or 0),
                fire_at=now + FOLLOWUP_DELAY,
                now=now,
            )
        except Exception as exc:  # defensive — never break the answer path
            logger.warning(
                "sales_followup_enqueue_failed",
                extra={
                    "trace_id": ctx.trace_id,
                    "chat_id": ctx.chat_id,
                    "error": repr(exc),
                },
            )

    async def _handle_greeting(
        self, *, question: str, ctx: AnswerContext
    ) -> AnswerResult:
        persona = self._persona_getter()
        system = _build_greeting_prompt(persona=persona)
        user = f"Сообщение клиента:\n{question}"

        try:
            payload = await self._openrouter.complete_json(
                system=system, user=user
            )
            extracted, next_question = _validate_payload(payload)
        except LlmSchemaViolation as exc:
            logger.warning(
                "sales_llm_schema_violation",
                extra={
                    "trace_id": ctx.trace_id,
                    "stage": STAGE_NEW,
                    "reason": str(exc),
                },
            )
            return _skip("llm_schema_violation")
        except Exception as exc:  # defensive — LLM transport failure
            logger.warning(
                "sales_llm_transport_error",
                extra={
                    "trace_id": ctx.trace_id,
                    "stage": STAGE_NEW,
                    "error": repr(exc),
                },
            )
            return _skip("llm_transport_error")

        merged = intent_merge(Intent(), extracted)
        # Greeting always transitions into scoping. Even if the customer
        # already supplied every field in the opener (unlikely), the next
        # turn handles the pitching transition cleanly.
        stage_after = STAGE_SCOPING
        await self._persist(
            ctx=ctx,
            current_stage=stage_after,
            intent=merged,
        )
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": STAGE_NEW,
                "stage_after": stage_after,
                "fields_extracted": sorted(
                    name
                    for name, value in merged.to_dict().items()
                    if value is not None
                ),
            },
        )
        return AnswerResult(
            handled=True,
            text=next_question,
            metadata={
                "answerer": NAME,
                "stage_before": STAGE_NEW,
                "stage_after": stage_after,
            },
        )

    async def _handle_scoping(
        self,
        *,
        question: str,
        ctx: AnswerContext,
        state: dict[str, Any],
    ) -> AnswerResult:
        persona = self._persona_getter()
        existing_intent = Intent.from_dict(state.get("collected_intent") or {})
        system = _build_scoping_prompt(persona=persona, intent=existing_intent)
        user = f"Сообщение клиента:\n{question}"

        try:
            payload = await self._openrouter.complete_json(
                system=system, user=user
            )
            extracted, next_question = _validate_payload(payload)
        except LlmSchemaViolation as exc:
            logger.warning(
                "sales_llm_schema_violation",
                extra={
                    "trace_id": ctx.trace_id,
                    "stage": STAGE_SCOPING,
                    "reason": str(exc),
                },
            )
            return _skip("llm_schema_violation")
        except Exception as exc:  # defensive — LLM transport failure
            logger.warning(
                "sales_llm_transport_error",
                extra={
                    "trace_id": ctx.trace_id,
                    "stage": STAGE_SCOPING,
                    "error": repr(exc),
                },
            )
            return _skip("llm_transport_error")

        merged = intent_merge(existing_intent, extracted)
        stage_after = STAGE_PITCHING if merged.is_complete() else STAGE_SCOPING
        await self._persist(
            ctx=ctx,
            current_stage=stage_after,
            intent=merged,
        )
        # Story 12.05 — scoping → pitching transition is the tour_preview
        # media moment. The textual pitch (LLM `next_question`) is still the
        # primary reply; the media dispatch is awaited so a failure can
        # append a textual fallback within the same turn.
        media_metadata: dict[str, Any] = {}
        final_text = next_question
        if stage_after == STAGE_PITCHING:
            media_metadata, dispatch_fallback = await self._fire_media_moment(
                ctx=ctx,
                intent=merged,
                purpose="tour_preview",
            )
            if dispatch_fallback:
                final_text = f"{next_question}\n{MATERIAL_DISPATCH_FALLBACK_LINE}"
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": STAGE_SCOPING,
                "stage_after": stage_after,
                "fields_extracted": sorted(
                    name
                    for name, value in extracted.items()
                    if value is not None
                ),
            },
        )
        return AnswerResult(
            handled=True,
            text=final_text,
            metadata={
                "answerer": NAME,
                "stage_before": STAGE_SCOPING,
                "stage_after": stage_after,
                **media_metadata,
            },
        )

    async def _maybe_handle_aside(
        self,
        *,
        question: str,
        ctx: AnswerContext,
        state: dict[str, Any],
    ) -> AnswerResult | None:
        """Run the per-turn classifier; route catalog/concept inline.

        Returns ``None`` when the turn is not an aside — the caller then
        continues into the stage-specific handler. Funnel state is never
        mutated by this method.
        """
        turn_intent = classify_turn(question, normalizer=self._normalizer)
        if turn_intent.kind == "catalog_ask":
            return await self._handle_catalog_ask(
                question=question, ctx=ctx, state=state
            )
        if turn_intent.kind == "concept_ask":
            return await self._handle_concept_ask(
                question=question,
                ctx=ctx,
                state=state,
                term=turn_intent.term or "",
            )
        if turn_intent.kind == "price_ask" and self._price_lookup is not None:
            return await self._handle_pricing(
                question=question, ctx=ctx, state=state
            )
        if _is_equipment_ask(question, normalizer=self._normalizer):
            return await self._handle_equipment_ask(
                ctx=ctx, state=state
            )
        return None

    async def _handle_equipment_ask(
        self,
        *,
        ctx: AnswerContext,
        state: dict[str, Any],
    ) -> AnswerResult:
        """Acknowledge an equipment ask + dispatch the equipment_gallery."""
        current_stage = str(state.get("current_stage") or "")
        intent = Intent.from_dict(state.get("collected_intent") or {})
        # Refresh `last_bot_msg_at` so the follow-up queue counts this turn.
        await self._persist(
            ctx=ctx, current_stage=current_stage, intent=intent
        )
        media_metadata, dispatch_fallback = await self._fire_media_moment(
            ctx=ctx,
            intent=intent,
            purpose="equipment_gallery",
        )
        ack = EQUIPMENT_ACK_LINE
        if dispatch_fallback:
            ack = f"{ack}\n{MATERIAL_DISPATCH_FALLBACK_LINE}"
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": current_stage,
                "stage_after": current_stage,
                "sales_turn_kind": "equipment_ask",
                **{
                    k: v
                    for k, v in media_metadata.items()
                    if k != "answerer"
                },
            },
        )
        return AnswerResult(
            handled=True,
            text=ack,
            metadata={
                "answerer": NAME,
                "stage_before": current_stage,
                "stage_after": current_stage,
                "sales_turn_kind": "equipment_ask",
                **media_metadata,
            },
        )

    async def _fire_media_moment(
        self,
        *,
        ctx: AnswerContext,
        intent: Intent,
        purpose: str,
    ) -> tuple[dict[str, Any], bool]:
        """Pick + dispatch a client material for the given purpose.

        Returns ``(metadata, dispatch_fallback)`` where ``metadata`` is the
        dict to merge into the ``AnswerResult.metadata`` and
        ``dispatch_fallback`` is ``True`` only when the dispatcher returned
        ``ok=False`` — the caller appends a textual fallback line in that
        case so the customer never sees a silent bot.
        """
        if self._material_selector is None or self._material_dispatcher is None:
            return {}, False
        project_id = int(ctx.project_id or 0)
        intent_tags = _intent_to_tags(intent)
        try:
            material = self._material_selector.pick(
                project_id=project_id,
                intent_tags=intent_tags,
                purpose=purpose,
            )
        except Exception as exc:  # defensive — never break the answer path
            logger.warning(
                "sales_material_pick_failed",
                extra={
                    "trace_id": ctx.trace_id,
                    "purpose": purpose,
                    "error": repr(exc),
                },
            )
            return {}, False
        if material is None:
            return {
                "sales_material_purpose": purpose,
                "sales_material_picked": False,
            }, False
        try:
            outcome = await self._material_dispatcher(
                chat_id=int(ctx.chat_id),  # type: ignore[arg-type]
                material_id=material.id,
                trace_id=ctx.trace_id,
                caption_override=None,
            )
        except Exception as exc:  # defensive — fall back to text
            logger.warning(
                "sales_material_dispatch_call_failed",
                extra={
                    "trace_id": ctx.trace_id,
                    "purpose": purpose,
                    "material_id": material.id,
                    "error": repr(exc),
                },
            )
            return {
                "sales_material_purpose": purpose,
                "sales_material_picked": True,
                "sales_material_dispatched": False,
                "sales_material_id": material.id,
            }, True
        dispatched_ok = bool(outcome.get("ok"))
        return {
            "sales_material_purpose": purpose,
            "sales_material_picked": True,
            "sales_material_dispatched": dispatched_ok,
            "sales_material_id": material.id,
        }, (not dispatched_ok)

    async def _handle_catalog_ask(
        self,
        *,
        question: str,
        ctx: AnswerContext,
        state: dict[str, Any],
    ) -> AnswerResult:
        project_id = int(ctx.project_id or 0)
        services = await asyncio.to_thread(
            self._services_repo.list_for_project, project_id=project_id
        )
        active_names = [
            row.name.strip()
            for row in services
            if getattr(row, "name", None) and row.name.strip()
        ]
        current_stage = str(state.get("current_stage") or "")
        if not active_names:
            logger.info(
                "sales_catalog_ask_empty",
                extra={
                    "trace_id": ctx.trace_id,
                    "project_id": project_id,
                    "hitl_reason": HITL_REASON_EMPTY_CATALOG,
                },
            )
            return AnswerResult(
                handled=True,
                text=EMPTY_CATALOG_ESCALATION_LINE,
                response_mode=RESPONSE_MODE_SALES_ESCALATION,
                metadata={
                    "answerer": NAME,
                    "stage_before": current_stage,
                    "stage_after": current_stage,
                    "sales_turn_kind": "catalog_empty",
                    "escalate": True,
                    "hitl_reason": HITL_REASON_EMPTY_CATALOG,
                },
            )

        names_block = "\n".join(f"• {name}" for name in active_names)
        text = _CATALOG_PROMPT_TEMPLATE.format(names_block=names_block).strip()
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": str(state.get("current_stage") or ""),
                "stage_after": str(state.get("current_stage") or ""),
                "sales_turn_kind": "catalog",
                "service_count": len(active_names),
            },
        )
        return AnswerResult(
            handled=True,
            text=text,
            metadata={
                "answerer": NAME,
                "stage_before": str(state.get("current_stage") or ""),
                "stage_after": str(state.get("current_stage") or ""),
                "sales_turn_kind": "catalog",
            },
        )

    async def _handle_concept_ask(
        self,
        *,
        question: str,
        ctx: AnswerContext,
        state: dict[str, Any],
        term: str,
    ) -> AnswerResult:
        # `classify_turn` already downgrades empty/punctuation-only terms
        # to ``other``, so by the time we get here the term is non-empty.
        term_clean = term.strip()
        project_id = int(ctx.project_id or 0)
        current_stage = str(state.get("current_stage") or "")
        service = await self._lookup_service_for_term(
            project_id=project_id, term=term_clean
        )
        if service is not None and (service.description or "").strip():
            description = (service.description or "").strip()
            logger.info(
                "sales_answerer_handled",
                extra={
                    "trace_id": ctx.trace_id,
                    "stage_before": current_stage,
                    "stage_after": current_stage,
                    "sales_turn_kind": "concept_op_desc",
                    "service_name": service.name,
                },
            )
            return AnswerResult(
                handled=True,
                text=description,
                metadata={
                    "answerer": NAME,
                    "stage_before": current_stage,
                    "stage_after": current_stage,
                    "sales_turn_kind": "concept_op_desc",
                    "service_name": service.name,
                },
            )

        return await self._answer_concept_via_rag(
            term=term_clean,
            ctx=ctx,
            current_stage=current_stage,
        )

    async def _lookup_service_for_term(
        self, *, project_id: int, term: str
    ) -> _ServiceRow | None:
        """Find a service whose name matches the customer's term.

        Two-pass match: exact case-insensitive first (cheap), then lemma-set
        equality across all services (handles Russian inflection like
        "Медовеевку Лайт" → "Медовеевка Лайт"). The lemma fallback only
        triggers when the exact lookup misses, so calls cost the same as
        before for the common nominative-case path.
        """
        exact = await asyncio.to_thread(
            self._services_repo.get_by_name,
            project_id=project_id,
            name=term,
        )
        if exact is not None:
            return exact

        # `classify_turn` filters all-punctuation terms, so the lemmatised
        # set is non-empty in practice; the loop below tolerates an empty
        # set anyway (every comparison would fail and we return ``None``).
        term_lemmas = set(self._normalizer.lemmas(term))
        services = await asyncio.to_thread(
            self._services_repo.list_for_project, project_id=project_id
        )
        for row in services:
            name = getattr(row, "name", None) or ""
            name_lemmas = set(self._normalizer.lemmas(name))
            if name_lemmas and name_lemmas == term_lemmas:
                return row
        return None

    async def _answer_concept_via_rag(
        self,
        *,
        term: str,
        ctx: AnswerContext,
        current_stage: str,
    ) -> AnswerResult:
        threshold = self._resolve_grounding_threshold(ctx)
        chunks: list[RagChunk] = []
        if self._rag is not None:
            chunks = await asyncio.to_thread(
                self._rag.retrieve,
                query=f"{term} определение",
                limit=3,
                project_id=ctx.project_id,
            )
        if chunks and chunks[0].score >= threshold:
            top = chunks[0]
            persona = self._persona_getter()
            system = _CONCEPT_RAG_PROMPT_TEMPLATE.format(
                persona=persona, term=term, chunk_text=top.chunk_text
            )
            user = f"Клиент спросил: «что такое {term}?»"
            try:
                payload = await self._openrouter.complete_json(
                    system=system, user=user
                )
            except Exception as exc:  # defensive — LLM transport failure
                logger.warning(
                    "sales_concept_rag_llm_error",
                    extra={
                        "trace_id": ctx.trace_id,
                        "term": term,
                        "error": repr(exc),
                    },
                )
                return self._escalate_concept_unknown(
                    term=term, ctx=ctx, current_stage=current_stage
                )
            text = ""
            if isinstance(payload, dict):
                raw = payload.get("text") or payload.get("next_question")
                if isinstance(raw, str):
                    text = raw.strip()
            if not text:
                logger.warning(
                    "sales_concept_rag_invalid_payload",
                    extra={
                        "trace_id": ctx.trace_id,
                        "term": term,
                    },
                )
                return self._escalate_concept_unknown(
                    term=term, ctx=ctx, current_stage=current_stage
                )
            logger.info(
                "sales_answerer_handled",
                extra={
                    "trace_id": ctx.trace_id,
                    "stage_before": current_stage,
                    "stage_after": current_stage,
                    "sales_turn_kind": "concept_rag",
                    "rag_top_score": top.score,
                },
            )
            return AnswerResult(
                handled=True,
                text=text,
                metadata={
                    "answerer": NAME,
                    "stage_before": current_stage,
                    "stage_after": current_stage,
                    "sales_turn_kind": "concept_rag",
                    "rag_top_score": top.score,
                },
            )
        return self._escalate_concept_unknown(
            term=term, ctx=ctx, current_stage=current_stage
        )

    def _escalate_concept_unknown(
        self,
        *,
        term: str,
        ctx: AnswerContext,
        current_stage: str,
    ) -> AnswerResult:
        logger.info(
            "sales_concept_escalation",
            extra={
                "trace_id": ctx.trace_id,
                "term": term,
                "stage": current_stage,
                "reason": "concept_unknown",
            },
        )
        return AnswerResult(
            handled=False,
            metadata={
                "skip_reason": "concept_unknown",
                "sales_turn_kind": "concept_unknown",
                "concept_term": term,
            },
        )

    def _resolve_grounding_threshold(self, ctx: AnswerContext) -> float:
        if self._grounding_threshold_getter is not None:
            try:
                return float(self._grounding_threshold_getter())
            except (TypeError, ValueError):
                return ctx.grounding_threshold
        return ctx.grounding_threshold

    async def _persist(
        self,
        *,
        ctx: AnswerContext,
        current_stage: str,
        intent: Intent,
        last_proposal: dict[str, Any] | None = None,
    ) -> None:
        now = self._clock()
        await asyncio.to_thread(
            lambda: self._state_repo.upsert(
                chat_id=int(ctx.chat_id),  # type: ignore[arg-type]
                project_id=int(ctx.project_id or 0),
                current_stage=current_stage,
                collected_intent=intent.to_dict(),
                last_proposal=last_proposal,
                last_bot_msg_at=now,
                now=now,
            )
        )

    async def _handle_pricing(
        self,
        *,
        question: str,
        ctx: AnswerContext,
        state: dict[str, Any],
    ) -> AnswerResult:
        """KB-first price quote; escalate-if-unknown.

        On hit: one LLM call wraps the price snippet into a one-sentence
        Russian reply. A regex verifier asserts the snippet's verbatim
        price token reappears in the reply — drift escalates instead of
        delivering a possibly-wrong price.

        On miss: skips the LLM entirely, returns the fixed Russian line,
        and signals a HITL ticket with ``reason='price_unknown'`` plus
        the structured payload so the operator sees the customer's
        verbatim question.
        """
        if self._price_lookup is None:
            return _skip("pricing_not_configured")
        current_stage = str(state.get("current_stage") or "")
        existing_intent = Intent.from_dict(state.get("collected_intent") or {})

        try:
            outcome = await self._price_lookup.lookup(
                project_id=ctx.project_id,
                intent=existing_intent,
                question=question,
            )
        except Exception as exc:  # defensive — RAG transport / sqlite error
            logger.warning(
                "sales_pricing_rag_unavailable",
                extra={
                    "trace_id": ctx.trace_id,
                    "error": repr(exc),
                },
            )
            return _skip("rag_unavailable")

        if isinstance(outcome, PriceFound):
            return await self._render_price_hit(
                outcome=outcome,
                ctx=ctx,
                intent=existing_intent,
                current_stage=current_stage,
            )

        return await self._escalate_price_unknown(
            missing=outcome,
            ctx=ctx,
            intent=existing_intent,
            current_stage=current_stage,
        )

    async def _render_price_hit(
        self,
        *,
        outcome: PriceFound,
        ctx: AnswerContext,
        intent: Intent,
        current_stage: str,
    ) -> AnswerResult:
        persona = self._persona_getter()
        system = _PRICING_HIT_PROMPT_TEMPLATE.format(
            persona=persona, snippet=outcome.snippet
        )
        user = f"Сообщение клиента:\n{outcome.snippet}"
        try:
            payload = await self._openrouter.complete_json(
                system=system, user=user
            )
        except Exception as exc:  # defensive — LLM transport failure
            logger.warning(
                "sales_pricing_llm_error",
                extra={
                    "trace_id": ctx.trace_id,
                    "error": repr(exc),
                },
            )
            return await self._escalate_price_quote_drift(
                ctx=ctx,
                intent=intent,
                current_stage=current_stage,
                snippet=outcome.snippet,
                source_chunk_id=outcome.source_chunk_id,
                drift_text=None,
            )

        text = ""
        if isinstance(payload, dict):
            raw = payload.get("text") or payload.get("next_question")
            if isinstance(raw, str):
                text = raw.strip()
        snippet_tokens = extract_price_tokens(outcome.snippet)
        if not text or not snippet_tokens or not any(
            token in text for token in snippet_tokens
        ):
            return await self._escalate_price_quote_drift(
                ctx=ctx,
                intent=intent,
                current_stage=current_stage,
                snippet=outcome.snippet,
                source_chunk_id=outcome.source_chunk_id,
                drift_text=text or None,
            )

        await self._persist(
            ctx=ctx, current_stage=STAGE_PRICING, intent=intent
        )
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": current_stage,
                "stage_after": STAGE_PRICING,
                "sales_turn_kind": "pricing_hit",
                "sales_price_source_chunk_id": outcome.source_chunk_id,
            },
        )
        return AnswerResult(
            handled=True,
            text=text,
            metadata={
                "answerer": NAME,
                "stage_before": current_stage,
                "stage_after": STAGE_PRICING,
                "sales_turn_kind": "pricing_hit",
                "sales_price_source_chunk_id": outcome.source_chunk_id,
            },
        )

    async def _escalate_price_quote_drift(
        self,
        *,
        ctx: AnswerContext,
        intent: Intent,
        current_stage: str,
        snippet: str,
        source_chunk_id: str,
        drift_text: str | None,
    ) -> AnswerResult:
        """LLM quote disagreed with the snippet — never deliver a wrong price.

        The bot says the fixed ``уточню у коллег…`` line and signals an
        operator handoff with ``price_unknown`` so the operator answers
        the question authoritatively.
        """
        logger.warning(
            "sales_price_quote_drift",
            extra={
                "trace_id": ctx.trace_id,
                "source_chunk_id": source_chunk_id,
                "snippet": snippet,
                "drift_text": drift_text,
            },
        )
        await self._persist(
            ctx=ctx,
            current_stage=STAGE_AWAITING_OPERATOR_PRICE,
            intent=intent,
        )
        return AnswerResult(
            handled=True,
            text=PRICING_MISS_FALLBACK,
            response_mode=RESPONSE_MODE_SALES_ESCALATION,
            metadata={
                "answerer": NAME,
                "stage_before": current_stage,
                "stage_after": STAGE_AWAITING_OPERATOR_PRICE,
                "sales_turn_kind": "pricing_quote_drift",
                "escalate": True,
                "hitl_reason": HITL_REASON_PRICE_UNKNOWN,
                "sales_price_source_chunk_id": source_chunk_id,
                "drift_text": drift_text,
            },
        )

    async def _escalate_price_unknown(
        self,
        *,
        missing: PriceMissing,
        ctx: AnswerContext,
        intent: Intent,
        current_stage: str,
    ) -> AnswerResult:
        await self._persist(
            ctx=ctx,
            current_stage=STAGE_AWAITING_OPERATOR_PRICE,
            intent=intent,
        )
        payload_dict = missing.payload.as_dict()
        logger.info(
            "sales_price_unknown",
            extra={
                "trace_id": ctx.trace_id,
                "payload": payload_dict,
                "stage_before": current_stage,
                "stage_after": STAGE_AWAITING_OPERATOR_PRICE,
            },
        )
        return AnswerResult(
            handled=True,
            text=PRICING_MISS_FALLBACK,
            response_mode=RESPONSE_MODE_SALES_ESCALATION,
            metadata={
                "answerer": NAME,
                "stage_before": current_stage,
                "stage_after": STAGE_AWAITING_OPERATOR_PRICE,
                "sales_turn_kind": "pricing_miss",
                "escalate": True,
                "hitl_reason": HITL_REASON_PRICE_UNKNOWN,
                "sales_price_unknown_payload": payload_dict,
            },
        )

    async def _handle_proposing(
        self,
        *,
        question: str,
        ctx: AnswerContext,
        state: dict[str, Any],
    ) -> AnswerResult:
        """Render an Epic-11 slot or escalate; handle acceptance / counters."""
        assert self._date_proposer is not None  # narrowed by caller
        existing_intent = Intent.from_dict(state.get("collected_intent") or {})
        last_proposal = state.get("last_proposal")

        # Acceptance only makes sense when a prior proposal exists. Otherwise
        # the customer's first sentence in ``proposing`` is the date hint, not
        # a confirmation.
        if last_proposal is not None and is_acceptance(
            question, normalizer=self._normalizer
        ):
            return await self._transition_to_closing(
                ctx=ctx, intent=existing_intent
            )

        now = self._clock()
        merged_intent = self._merge_dates_from_customer_message(
            existing_intent=existing_intent,
            question=question,
            now=now,
        )

        result = await self._date_proposer.propose(
            project_id=int(ctx.project_id or 0),
            intent=merged_intent,
            now=now,
        )
        if isinstance(result, Proposal):
            return await self._render_and_persist_proposal(
                proposal=result,
                ctx=ctx,
                intent=merged_intent,
            )
        return await self._handle_no_proposal(
            no_proposal=result,
            ctx=ctx,
            intent=merged_intent,
        )

    def _merge_dates_from_customer_message(
        self,
        *,
        existing_intent: Intent,
        question: str,
        now: datetime,
    ) -> Intent:
        """If the customer's turn carries a parseable date, override ``dates``.

        Counter-offers must update the proposer's window; otherwise we'd
        re-propose the old slot. The merge is conservative — when the
        question has no parseable date, the existing ``dates`` value
        stands.
        """
        parsed = parse_russian_date_span(question, now=now.date())
        if parsed is None:
            return existing_intent
        return replace(existing_intent, dates=question.strip())

    async def _render_and_persist_proposal(
        self,
        *,
        proposal: Proposal,
        ctx: AnswerContext,
        intent: Intent,
    ) -> AnswerResult:
        date_str = _format_proposal_date(proposal.date_iso)
        start_time = proposal.start_time_iso
        persona = self._persona_getter()
        system = _PROPOSAL_PROMPT_TEMPLATE.format(
            persona=persona, date=date_str, start_time=start_time
        )
        user = (
            "Озвучь клиенту дату {date} с началом в {start_time}.".format(
                date=date_str, start_time=start_time
            )
        )
        try:
            payload = await self._openrouter.complete_json(
                system=system, user=user
            )
        except Exception as exc:  # defensive — LLM transport failure
            logger.warning(
                "sales_proposal_llm_error",
                extra={
                    "trace_id": ctx.trace_id,
                    "error": repr(exc),
                },
            )
            return await self._escalate_proposal_drift(
                ctx=ctx,
                intent=intent,
                proposal=proposal,
                drift_text=None,
                expected_date=date_str,
                expected_time=start_time,
            )

        text = ""
        if isinstance(payload, dict):
            raw = payload.get("text") or payload.get("next_question")
            if isinstance(raw, str):
                text = raw.strip()
        if not self._proposal_text_matches(
            text, date_str=date_str, start_time=start_time
        ):
            return await self._escalate_proposal_drift(
                ctx=ctx,
                intent=intent,
                proposal=proposal,
                drift_text=text,
                expected_date=date_str,
                expected_time=start_time,
            )

        await self._persist(
            ctx=ctx,
            current_stage=STAGE_PROPOSING,
            intent=intent,
            last_proposal=proposal.as_dict(),
        )
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": STAGE_PROPOSING,
                "stage_after": STAGE_PROPOSING,
                "sales_turn_kind": "proposal",
                "proposal_date": proposal.date_iso,
                "proposal_start": proposal.start_time_iso,
            },
        )
        return AnswerResult(
            handled=True,
            text=text,
            metadata={
                "answerer": NAME,
                "stage_before": STAGE_PROPOSING,
                "stage_after": STAGE_PROPOSING,
                "sales_turn_kind": "proposal",
                "proposal": proposal.as_dict(),
            },
        )

    @staticmethod
    def _proposal_text_matches(
        text: str, *, date_str: str, start_time: str
    ) -> bool:
        """Verifier guardrail: the LLM must keep date + time verbatim.

        Mirrors the regex check called out in the story so an LLM
        hallucination ("около 14:30") cannot reach the customer.
        """
        if not text:
            return False
        if date_str not in text:
            return False
        if start_time not in text:
            return False
        # Defensive: a stray time like "14:30" would indicate drift even
        # when the canonical "14:00" is also present. Extract all H:MM /
        # HH:MM substrings and require they all equal ``start_time``.
        time_matches = re.findall(r"\d{1,2}:\d{2}", text)
        if any(match != start_time for match in time_matches):
            return False
        return True

    async def _escalate_proposal_drift(
        self,
        *,
        ctx: AnswerContext,
        intent: Intent,
        proposal: Proposal,
        drift_text: str | None,
        expected_date: str,
        expected_time: str,
    ) -> AnswerResult:
        logger.warning(
            "sales_proposal_drift",
            extra={
                "trace_id": ctx.trace_id,
                "expected_date": expected_date,
                "expected_time": expected_time,
                "drift_text": drift_text,
                "proposal_service_id": proposal.service_id,
            },
        )
        # Keep the customer-facing line safe — never deliver the drifted text.
        await self._persist(
            ctx=ctx,
            current_stage=STAGE_PROPOSING,
            intent=intent,
        )
        return AnswerResult(
            handled=True,
            text=PROPOSAL_FALLBACK_UNAVAILABLE,
            response_mode=RESPONSE_MODE_SALES_ESCALATION,
            metadata={
                "answerer": NAME,
                "stage_before": STAGE_PROPOSING,
                "stage_after": STAGE_PROPOSING,
                "escalate": True,
                "hitl_reason": HITL_REASON_PROPOSAL_DRIFT,
                "expected_date": expected_date,
                "expected_time": expected_time,
                "drift_text": drift_text,
            },
        )

    async def _handle_no_proposal(
        self,
        *,
        no_proposal: NoProposal,
        ctx: AnswerContext,
        intent: Intent,
    ) -> AnswerResult:
        reason = no_proposal.reason
        if reason == NO_PROPOSAL_AMBIGUOUS_SERVICE:
            await self._persist(
                ctx=ctx, current_stage=STAGE_PROPOSING, intent=intent
            )
            logger.info(
                "sales_proposal_ambiguous_service",
                extra={"trace_id": ctx.trace_id},
            )
            return AnswerResult(
                handled=True,
                text=PROPOSAL_AMBIGUOUS_SERVICE_CLARIFIER,
                metadata={
                    "answerer": NAME,
                    "stage_before": STAGE_PROPOSING,
                    "stage_after": STAGE_PROPOSING,
                    "sales_turn_kind": "proposal_ambiguous_service",
                },
            )

        if reason == NO_PROPOSAL_NO_DATE_HINT:
            # The customer is in proposing but hasn't pinned a date yet —
            # ask for one (no escalation, no calendar leak).
            await self._persist(
                ctx=ctx, current_stage=STAGE_PROPOSING, intent=intent
            )
            return AnswerResult(
                handled=True,
                text="Какую дату хотите?",
                metadata={
                    "answerer": NAME,
                    "stage_before": STAGE_PROPOSING,
                    "stage_after": STAGE_PROPOSING,
                    "sales_turn_kind": "proposal_no_date_hint",
                },
            )

        if reason == NO_PROPOSAL_CALENDAR_NOT_ENABLED:
            return await self._escalate_with_fallback(
                ctx=ctx,
                intent=intent,
                text=PROPOSAL_FALLBACK_CALENDAR_DISABLED,
                hitl_reason=HITL_REASON_CALENDAR_DISABLED,
                metadata_kind="proposal_calendar_disabled",
            )

        # Remaining reasons (provider_error / no_slots_in_window) share a
        # customer-facing line and a generic HITL reason so the operator
        # sees that a date confirmation is pending without leaking
        # backend-failure detail.
        return await self._escalate_with_fallback(
            ctx=ctx,
            intent=intent,
            text=PROPOSAL_FALLBACK_UNAVAILABLE,
            hitl_reason=HITL_REASON_PROPOSAL_FAILED,
            metadata_kind=(
                "proposal_provider_error"
                if reason == NO_PROPOSAL_PROVIDER_ERROR
                else "proposal_no_slots"
            ),
        )

    async def _escalate_with_fallback(
        self,
        *,
        ctx: AnswerContext,
        intent: Intent,
        text: str,
        hitl_reason: str,
        metadata_kind: str,
    ) -> AnswerResult:
        await self._persist(
            ctx=ctx, current_stage=STAGE_PROPOSING, intent=intent
        )
        logger.info(
            "sales_proposal_escalation",
            extra={
                "trace_id": ctx.trace_id,
                "hitl_reason": hitl_reason,
            },
        )
        return AnswerResult(
            handled=True,
            text=text,
            response_mode=RESPONSE_MODE_SALES_ESCALATION,
            metadata={
                "answerer": NAME,
                "stage_before": STAGE_PROPOSING,
                "stage_after": STAGE_PROPOSING,
                "escalate": True,
                "hitl_reason": hitl_reason,
                "sales_turn_kind": metadata_kind,
            },
        )

    async def _transition_to_closing(
        self, *, ctx: AnswerContext, intent: Intent
    ) -> AnswerResult:
        """Customer accepted the proposal — speak the handoff line + escalate.

        The transition + the customer-facing line + the HITL handoff all
        happen on the same turn; the state row is moved to ``closing`` so
        a subsequent follow-up resumes from the right spot.
        """
        await self._persist(
            ctx=ctx,
            current_stage=STAGE_CLOSING,
            intent=intent,
        )
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": STAGE_PROPOSING,
                "stage_after": STAGE_CLOSING,
                "sales_turn_kind": "acceptance",
                "hitl_reason": HITL_REASON_CLOSING_HANDOFF,
            },
        )
        return AnswerResult(
            handled=True,
            text=CLOSING_HANDOFF_LINE,
            response_mode=RESPONSE_MODE_SALES_ESCALATION,
            metadata={
                "answerer": NAME,
                "stage_before": STAGE_PROPOSING,
                "stage_after": STAGE_CLOSING,
                "sales_turn_kind": "acceptance",
                "escalate": True,
                "hitl_reason": HITL_REASON_CLOSING_HANDOFF,
            },
        )

    async def _handle_closing(
        self,
        *,
        question: str,
        ctx: AnswerContext,
        state: dict[str, Any],
    ) -> AnswerResult:
        """Closing-stage follow-ups stay in closing — the handoff is sticky."""
        existing_intent = Intent.from_dict(state.get("collected_intent") or {})
        await self._persist(
            ctx=ctx, current_stage=STAGE_CLOSING, intent=existing_intent
        )
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": STAGE_CLOSING,
                "stage_after": STAGE_CLOSING,
                "sales_turn_kind": "closing_followup",
                "hitl_reason": HITL_REASON_CLOSING_HANDOFF,
            },
        )
        return AnswerResult(
            handled=True,
            text=CLOSING_HANDOFF_LINE,
            response_mode=RESPONSE_MODE_SALES_ESCALATION,
            metadata={
                "answerer": NAME,
                "stage_before": STAGE_CLOSING,
                "stage_after": STAGE_CLOSING,
                "sales_turn_kind": "closing_followup",
                "escalate": True,
                "hitl_reason": HITL_REASON_CLOSING_HANDOFF,
            },
        )


__all__ = [
    "CLOSING_HANDOFF_LINE",
    "EMPTY_CATALOG_ESCALATION_LINE",
    "EQUIPMENT_ACK_LINE",
    "HITL_REASON_CALENDAR_DISABLED",
    "HITL_REASON_CLOSING_HANDOFF",
    "HITL_REASON_EMPTY_CATALOG",
    "HITL_REASON_PRICE_UNKNOWN",
    "HITL_REASON_PROPOSAL_DRIFT",
    "HITL_REASON_PROPOSAL_FAILED",
    "LlmSchemaViolation",
    "MATERIAL_DISPATCH_FALLBACK_LINE",
    "MaterialDispatcher",
    "NAME",
    "PRICING_MISS_FALLBACK",
    "PROPOSAL_AMBIGUOUS_SERVICE_CLARIFIER",
    "PROPOSAL_FALLBACK_CALENDAR_DISABLED",
    "PROPOSAL_FALLBACK_UNAVAILABLE",
    "RESPONSE_MODE_SALES_ESCALATION",
    "STAGE_AWAITING_OPERATOR_PRICE",
    "STAGE_CLOSING",
    "STAGE_PITCHING",
    "STAGE_PRICING",
    "STAGE_PROPOSING",
    "STAGE_SCOPING",
    "SalesPersonaAnswerer",
]

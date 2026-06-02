"""``CalendarAvailabilityAnswerer`` — the calendar feature's pipeline stage
(Epic 11, story 11.07).

Sits **before** ``GroundedRagAnswerer`` in the ``AnswerPipeline``. It owns
availability questions for calendar-enabled projects and answers them in
Russian; everything else falls through untouched. Orchestration order (each
step gated cheaply before the next, per project-context):

1. **Gate (cheapest first):** ``settings_repo.is_enabled`` — a project with no
   calendar config reads disabled and the answerer ``_skip``s immediately, with
   NO intent detection and NO API call. This is the opt-in tri-state's "off"
   leg: a silent no-op that adds zero latency.
2. **Intent:** reuse the shared scheduling-intent regex (``has_scheduling_intent``
   on normalized text) — non-scheduling messages ``_skip``.
3. **Service resolve (FR-22):** ``NoMatch`` / ``Ambiguous`` / no parseable time
   → ask ONE Russian clarifying question (a *handled* result) and arm a one-turn
   flag for the chat. On the next still-unresolved inbound the flag is set, so
   the answerer escalates instead of looping.
4. **Connected?:** no token provider / freebusy client wired, or the operator's
   token is dead (``CalendarReconnectNeeded``) → escalate (never a 500, never a
   guess).
5. **Compute:** ``get_access_token`` → ONE ``query_busy`` over the lookahead
   window → ``compute_availability`` in the project timezone → a Russian
   available / not-available answer.
6. **Failure:** ``CalendarReconnectNeeded`` / ``CalendarProviderError`` /
   ``TokenNotFound`` → escalate to HITL routed to the project's calendar
   operator with context; never fabricate an availability answer.

**Answerers dispatch, they don't error:** "not my intent" / disabled returns
``handled=False``; "my intent but the backend failed" returns a *handled*
escalation result (``response_mode="calendar_escalation"``) so the inbound
handler routes a ticket to the calendar operator — it never silently falls
through to the LLM with wrong context. Calendar event titles are never echoed;
only free/busy-derived availability reaches the customer.

Customer-facing copy is Russian (illustrative defaults here; tunable as data
per project-context). Logs carry ``trace_id`` and never tokens or event content.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from services.api.app.answerers import AnswerContext, AnswerResult
from services.api.app.answerers.scheduling_context import has_scheduling_intent
from services.api.app.calendar.access_token_cache import CalendarReconnectNeeded
from services.api.app.calendar.availability import (
    compute_availability,
    parse_service_rule,
)
from services.api.app.calendar.calendar_client import CalendarProviderError
from services.api.app.calendar.service_resolver import (
    Ambiguous,
    NoMatch,
    Resolved,
    extract_requested_start,
    resolve_service,
)
from services.api.app.calendar.settings_repository import (
    CalendarProjectSettings,
    ServiceRule,
)
from services.api.app.calendar.token_repository import TokenNotFound
from services.api.app.russian_text.normalizer import RussianNormalizer
from services.api.app.sales.russian_sales_intent import is_sales_intent

logger = logging.getLogger(__name__)

# Escalation marker on the handled AnswerResult so the inbound handler routes a
# HITL ticket to the calendar operator (rather than delivering ``text``).
RESPONSE_MODE_ESCALATION = "calendar_escalation"
RESPONSE_MODE_ANSWER = "calendar_availability"

# --- Illustrative Russian copy (configurable as data per project-context). ---
CLARIFY_NO_SERVICE = (
    "Подскажите, пожалуйста, на какую услугу и на какое время вы хотите записаться?"
)
CLARIFY_AMBIGUOUS = (
    "Уточните, пожалуйста, какая именно услуга вам нужна: {options}?"
)
CLARIFY_NO_TIME = (
    "Подскажите, пожалуйста, на какой день и время вы хотите записаться?"
)
ANSWER_AVAILABLE = "Да, это время свободно — можно записаться."
ANSWER_NOT_AVAILABLE = "К сожалению, это время недоступно для записи."
NOT_CONNECTED_NOTE = "availability requested but operator calendar not connected"
ESCALATION_CONTEXT = "availability question; calendar error/uncertainty"


class _SettingsRepo(Protocol):
    def is_enabled(self, project_id: int) -> bool: ...
    def get(self, project_id: int) -> CalendarProjectSettings | None: ...
    def list_service_rules(self, project_id: int) -> list[ServiceRule]: ...


class _TokenProvider(Protocol):
    async def get_access_token(
        self,
        project_id: int,
        operator: str,
        *,
        operator_chat_id: int,
        trace_id: str,
    ) -> str: ...


class _FreeBusyClient(Protocol):
    async def query_busy(
        self,
        *,
        access_token: str,
        calendar_id: str = "primary",
        time_min: Any,
        time_max: Any,
        trace_id: str,
    ) -> Any: ...


class _ClarifyStore(Protocol):
    def is_armed(self, chat_id: int) -> bool: ...
    def arm(self, chat_id: int, *, trace_id: str) -> None: ...
    def clear(self, chat_id: int) -> None: ...


class _OperatorChatResolver(Protocol):
    def __call__(self, operator: str) -> int | None: ...


class CalendarAvailabilityAnswerer:
    name = "calendar_availability"

    def __init__(
        self,
        *,
        settings_repo: _SettingsRepo,
        token_provider: _TokenProvider | None,
        freebusy_client: _FreeBusyClient | None,
        normalizer: RussianNormalizer,
        clarify_store: _ClarifyStore,
        operator_chat_resolver: _OperatorChatResolver,
    ) -> None:
        self._settings = settings_repo
        self._token_provider = token_provider
        self._freebusy = freebusy_client
        self._normalizer = normalizer
        self._clarify = clarify_store
        self._operator_chat_resolver = operator_chat_resolver

    async def try_answer(
        self, *, question: str, ctx: AnswerContext
    ) -> AnswerResult:
        project_id = ctx.project_id
        if project_id is None:
            return self._skip(reason="no_project_id", ctx=ctx)

        # (1) Cheap gate FIRST — no intent work, no API call when disabled.
        enabled = await asyncio.to_thread(self._settings.is_enabled, project_id)
        if not enabled:
            return self._skip(reason="calendar_not_enabled", ctx=ctx)

        # (2) Intent — reuse the shared scheduling-intent seam.
        normalized = self._normalizer.normalize(question)
        if not has_scheduling_intent(normalized):
            return self._skip(reason="not_scheduling_intent", ctx=ctx)

        project_settings = await asyncio.to_thread(self._settings.get, project_id)
        rules = await asyncio.to_thread(
            self._settings.list_service_rules, project_id
        )
        project_tz = self._project_tz(project_settings)

        # (3) Service + time resolution (FR-22 one clarifying turn).
        match = resolve_service(
            text=question, service_rules=rules, normalizer=self._normalizer
        )
        requested_start = extract_requested_start(
            text=question, now=ctx.now, project_tz=project_tz
        )
        if isinstance(match, NoMatch):
            # Story 12.37 (D11) — a sales-intent booking whose service this legacy
            # resolver can't match (e.g. the project's multi-word service name)
            # must NOT get the "на какую услугу и на какое время…" clarify even
            # when the customer named both. Defer it to the SalesPersonaAnswerer
            # (the booking owner) → the inbound endpoint escalates to a human. A
            # non-sales availability ask ("свободно ли завтра?") still clarifies.
            if is_sales_intent(question, normalizer=self._normalizer):
                return self._skip(
                    reason="sales_intent_defer_to_persona", ctx=ctx
                )
            return await self._clarify_or_escalate(
                ctx=ctx,
                clarify_text=CLARIFY_NO_SERVICE,
                reason="service_no_match",
                project_settings=project_settings,
            )
        if isinstance(match, Ambiguous):
            options = ", ".join(
                rule.name for rule in match.candidates if rule.name
            )
            return await self._clarify_or_escalate(
                ctx=ctx,
                clarify_text=CLARIFY_AMBIGUOUS.format(options=options),
                reason="service_ambiguous",
                project_settings=project_settings,
            )
        if requested_start is None:
            return await self._clarify_or_escalate(
                ctx=ctx,
                clarify_text=CLARIFY_NO_TIME,
                reason="no_requested_time",
                project_settings=project_settings,
            )

        # Resolved + a concrete time — the customer gave us a clear request, so
        # any earlier clarify flag is stale; drop it before answering.
        await self._clear_clarify(ctx)

        # (4) Connected? — need settings + the operator + a wired provider/client
        # + chat id. A connected project always has a settings row, so a missing
        # one (or operator) is itself the "not connected" leg.
        operator = project_settings.calendar_operator if project_settings else None
        if (
            project_settings is None
            or self._token_provider is None
            or self._freebusy is None
            or not operator
        ):
            return self._escalate(
                ctx=ctx,
                reason="calendar_not_connected",
                project_settings=project_settings,
            )
        operator_chat_id = self._operator_chat_resolver(operator)
        if operator_chat_id is None:
            return self._escalate(
                ctx=ctx,
                reason="operator_chat_id_unknown",
                project_settings=project_settings,
            )

        # (5) Compute — token → ONE freebusy call → pure availability engine.
        resolved_match: Resolved = match
        return await self._compute_answer(
            ctx=ctx,
            project_id=project_id,
            operator=operator,
            operator_chat_id=operator_chat_id,
            requested_start=requested_start,
            service_rule=resolved_match.service,
            project_settings=project_settings,
            project_tz=project_tz,
            lookahead_days=project_settings.lookahead_days,
        )

    async def _compute_answer(
        self,
        *,
        ctx: AnswerContext,
        project_id: int,
        operator: str,
        operator_chat_id: int,
        requested_start,
        service_rule: ServiceRule,
        project_settings: CalendarProjectSettings | None,
        project_tz: ZoneInfo,
        lookahead_days: int,
    ) -> AnswerResult:
        assert self._token_provider is not None  # narrowed by caller
        assert self._freebusy is not None
        try:
            access_token = await self._token_provider.get_access_token(
                project_id,
                operator,
                operator_chat_id=operator_chat_id,
                trace_id=ctx.trace_id,
            )
            time_min = ctx.now
            time_max = ctx.now + timedelta(days=lookahead_days)
            free_busy = await self._freebusy.query_busy(
                access_token=access_token,
                time_min=time_min,
                time_max=time_max,
                trace_id=ctx.trace_id,
            )
        except CalendarReconnectNeeded:
            return self._escalate(
                ctx=ctx,
                reason="reconnect_needed",
                project_settings=project_settings,
            )
        except TokenNotFound:
            return self._escalate(
                ctx=ctx,
                reason="token_not_found",
                project_settings=project_settings,
            )
        except CalendarProviderError:
            return self._escalate(
                ctx=ctx,
                reason="provider_error",
                project_settings=project_settings,
            )

        parsed_rule = parse_service_rule(
            service_rule,
            lookahead_days=lookahead_days,
            country_code=ctx.country_code,
        )
        result = compute_availability(
            now=ctx.now,
            requested_start=requested_start,
            busy=free_busy.busy,
            service_rule=parsed_rule,
            project_tz=project_tz,
        )
        logger.info(
            "calendar_availability_computed",
            extra={
                "trace_id": ctx.trace_id,
                "available": result.available,
                "reason": result.reason,
                "busy_blocks": len(free_busy.busy),
            },
        )
        text = ANSWER_AVAILABLE if result.available else ANSWER_NOT_AVAILABLE
        return AnswerResult(
            handled=True,
            text=text,
            response_mode=RESPONSE_MODE_ANSWER,
            metadata={
                "available": result.available,
                "reason": result.reason,
            },
        )

    async def _clarify_or_escalate(
        self,
        *,
        ctx: AnswerContext,
        clarify_text: str,
        reason: str,
        project_settings: CalendarProjectSettings | None,
    ) -> AnswerResult:
        """Ask exactly once (FR-22); on a second unresolved turn, escalate.

        Without a ``chat_id`` we cannot track the one-turn clarify state, so we
        cannot honor the FR-22 contract — escalate immediately rather than risk
        looping clarify on every inbound.
        """
        if ctx.chat_id is None:
            return self._escalate(
                ctx=ctx,
                reason=f"{reason}_no_chat_id",
                project_settings=project_settings,
            )
        already_asked = await asyncio.to_thread(
            self._clarify.is_armed, ctx.chat_id
        )
        if already_asked:
            return self._escalate(
                ctx=ctx,
                reason=f"{reason}_after_clarify",
                project_settings=project_settings,
            )
        await asyncio.to_thread(
            self._clarify.arm, ctx.chat_id, trace_id=ctx.trace_id
        )
        logger.info(
            "calendar_availability_clarify",
            extra={"trace_id": ctx.trace_id, "reason": reason},
        )
        return AnswerResult(
            handled=True,
            text=clarify_text,
            response_mode=RESPONSE_MODE_ANSWER,
            metadata={"clarify": True, "reason": reason},
        )

    def _escalate(
        self,
        *,
        ctx: AnswerContext,
        reason: str,
        project_settings: CalendarProjectSettings | None,
    ) -> AnswerResult:
        """A *handled* HITL escalation routed to the project's calendar operator.

        Carries the routing target + context in metadata so the inbound handler
        opens/continues a ticket assigned to the calendar operator instead of
        delivering customer-facing text.
        """
        operator = (
            project_settings.calendar_operator if project_settings else None
        )
        logger.info(
            "calendar_availability_escalated",
            extra={
                "trace_id": ctx.trace_id,
                "reason": reason,
                "calendar_operator": operator,
            },
        )
        return AnswerResult(
            handled=True,
            text=None,
            response_mode=RESPONSE_MODE_ESCALATION,
            metadata={
                "escalate": True,
                "reason": reason,
                "calendar_operator": operator,
                "escalation_context": ESCALATION_CONTEXT,
            },
        )

    async def _clear_clarify(self, ctx: AnswerContext) -> None:
        if ctx.chat_id is not None:
            await asyncio.to_thread(self._clarify.clear, ctx.chat_id)

    @staticmethod
    def _project_tz(project_settings: CalendarProjectSettings | None) -> ZoneInfo:
        timezone = (
            project_settings.project_timezone
            if project_settings is not None
            else "Europe/Moscow"
        )
        return ZoneInfo(timezone)

    def _skip(self, *, reason: str, ctx: AnswerContext) -> AnswerResult:
        logger.info(
            "calendar_availability_skipped",
            extra={"trace_id": ctx.trace_id, "reason": reason},
        )
        return AnswerResult(handled=False)

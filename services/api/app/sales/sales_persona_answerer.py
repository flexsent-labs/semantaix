"""SalesPersonaAnswerer - greeting, scoping, asides, proposing, closing.

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
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo

from services.api.app.answerers import AnswerContext, AnswerResult
from services.api.app.calendar.availability import (
    REASON_DATE_EXCEPTION,
    REASON_IN_PAST,
    REASON_OUTSIDE_WORKING_HOURS,
    REASON_WRONG_SERVICE_DAY,
)
from services.api.app.calendar.requested_time_check import (
    STATUS_AVAILABLE,
    STATUS_ERROR,
    STATUS_NOT_CONNECTED,
    STATUS_UNAVAILABLE,
    check_requested_availability,
)
from services.api.app.calendar.service_resolver import (
    extract_all_clocks,
    extract_requested_date,
    extract_requested_start,
    names_invalid_date,
)
from services.api.app.rag import RagChunk
from services.api.app.sales.acceptance import is_acceptance
from services.api.app.sales.cancel_intent import is_cancellation
from services.api.app.sales.client_materials_repository import ClientMaterial
from services.api.app.sales.closure import is_no_more
from services.api.app.sales.date_parser import parse_russian_date_span
from services.api.app.sales.date_proposer import (
    NO_PROPOSAL_AMBIGUOUS_SERVICE,
    NO_PROPOSAL_CALENDAR_NOT_ENABLED,
    NO_PROPOSAL_NO_DATE_HINT,
    NO_PROPOSAL_PROVIDER_ERROR,
    NoProposal,
    Proposal,
)
from services.api.app.sales.decline import is_decline
from services.api.app.sales.intent import _FIELD_NAMES, Intent, intent_merge
from services.api.app.sales.out_of_scope import is_out_of_scope
from services.api.app.sales.price_lookup import (
    PriceFound,
    PriceMissing,
    extract_price_tokens,
)
from services.api.app.sales.reply_language import (
    detect_language,
    localize,
    reply_language_directive,
)
from services.api.app.sales.russian_sales_intent import is_sales_intent
from services.api.app.sales.scoping_schema import (
    TRANSFER_SCHEMA,
    ScopingSchema,
)
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
# Story 12.46 (round-9 R9-1) - a leading greeting marks "the customer moved on
# from pricing", so we re-enter the funnel instead of staying stuck in pricing.
_GREETING_RE = re.compile(
    r"^\s*(здравствуй|привет|добр(ый|ое|ого)\s*(день|вечер|утро)?"
    r"|hello|hi|hey|good\s+(morning|afternoon|evening))",
    re.IGNORECASE,
)
# Story 12.10 — scoping is complete but the booking carries no concrete time and
# the calendar can actually check it: we ask for the date+time and park here for
# exactly one turn (the stage itself is the "already asked" marker).
STAGE_AWAITING_TIME = "awaiting_time"

RESPONSE_MODE_SALES_ESCALATION = "sales_escalation"

HITL_REASON_CALENDAR_DISABLED = "date_calendar_disabled"
HITL_REASON_PROPOSAL_DRIFT = "sales_proposal_drift"
HITL_REASON_PROPOSAL_FAILED = "sales_proposal_failed"
HITL_REASON_CLOSING_HANDOFF = "sales_closing_handoff"
HITL_REASON_PRICE_UNKNOWN = "price_unknown"
HITL_REASON_EMPTY_CATALOG = "catalog_empty"
HITL_REASON_SCOPING_COMPLETE = "sales_scoping_complete"
# Story 12.27 — a customer asking to cancel a booking. There is no
# booking-of-record the bot can cancel autonomously (operators finalize
# bookings), so the request is acknowledged and routed to a human.
HITL_REASON_CANCELLATION = "sales_cancellation_request"
# Story 12.32 (D1) — a concrete requested time was given but the calendar could
# not be consulted (not connected / reconnect needed / provider error). The
# booking is escalated flagged UNVERIFIED so the operator confirms the exact
# time (and reconnects the calendar if needed) — never a silent accept.
HITL_REASON_CALENDAR_UNVERIFIED = "sales_calendar_unverified"
# Story 12.59 (round-14) - a capacity / "how many vehicles" question the bot
# can't answer from the catalog (no per-vehicle capacity data). Escalated so a
# human answers, with question-appropriate copy (never the thank-you handoff).
HITL_REASON_CAPACITY = "sales_capacity_question"

# Customer-facing Russian copy for the proposing / closing branches. Kept
# inline as named constants — short, fixed strings, no LLM in the loop for
# the fallback cases.
PROPOSAL_FALLBACK_CALENDAR_DISABLED = "Дату подтвержу у коллег."
PROPOSAL_FALLBACK_UNAVAILABLE = "Уточню свободные даты и сразу сообщу."
PROPOSAL_AMBIGUOUS_SERVICE_CLARIFIER = "На каком туре остановимся?"
CLOSING_HANDOFF_LINE = "Передам коллегам для подтверждения, на связи."
# Story 12.52 (round-11 N3) — English variants for the remaining funnel-reachable
# deterministic lines (closing / cancellation / empty-catalog), so an English
# thread that reaches a closing or cancellation handoff stays English.
CLOSING_HANDOFF_LINE_EN = (
    "I'll pass this to my colleagues for confirmation - talk soon."
)
# Story 12.27 — cancellation request. The customer-facing line does not presume
# the cancellation is done (a human confirms it); the operator-facing context
# tags the ticket so the human sees it's an отмена, not a new booking.
CANCELLATION_HANDOFF_LINE = (
    "Передам вашу просьбу об отмене коллеге - свяжутся с вами."
)
CANCELLATION_HANDOFF_LINE_EN = (
    "I'll pass your cancellation request to a colleague - they'll get in touch."
)
CANCELLATION_ESCALATION_CONTEXT = "Запрос на отмену брони"
# Story 12.34 (D7) — an out-of-scope request (dining/lodging) is politely
# declined and redirected to buggy bookings, never accepted as a booking.
OUT_OF_SCOPE_DECLINE_LINE = (
    "Этим, к сожалению, не помогу - я по прокату багги. "
    "Подскажу с поездкой: даты и сколько человек?"
)
OUT_OF_SCOPE_DECLINE_LINE_EN = (
    "I'm afraid I can't help with that - I handle buggy rentals. "
    "I can help with a trip: what dates, and how many people?"
)
# Story 12.54 (round-12 D5) — appended to a booking reply when the SAME message
# also carried an out-of-scope ask (a mixed "ресторан + запишите на багги"), so
# the off-topic part is declined in one line WITHOUT re-asking booking fields
# (the full OUT_OF_SCOPE_DECLINE_LINE's "даты?" would duplicate the funnel).
MIXED_OUT_OF_SCOPE_SUFFIX = (
    "А с остальным, к сожалению, не помогу - я по прокату багги."
)
MIXED_OUT_OF_SCOPE_SUFFIX_EN = (
    "As for the rest, I'm afraid I can't help - I handle buggy rentals."
)
# Story 12.59 (round-14) - a capacity question is answered as "I'm finding out",
# NOT thanked-and-handed-off. Frames the HITL escalation as checking options.
CAPACITY_ESCALATION_LINE = "Уточняю у коллег, какие варианты есть, и сразу сообщу."
CAPACITY_ESCALATION_LINE_EN = (
    "Let me check the options with my colleagues and get right back to you."
)
# Story 16 (round-16 R16-1) — an impossible calendar date («31 июня», «31.06»)
# is rejected with a clarify, never accepted as a booking.
INVALID_DATE_CLARIFY_LINE = (
    "Такой даты не существует. Уточните, пожалуйста, желаемую дату."
)
INVALID_DATE_CLARIFY_LINE_EN = (
    "That date doesn't exist. Could you confirm the date you'd like?"
)
# Story 16 (round-16 R16-4) — gratitude / smalltalk gets a courteous ack, not a
# booking-handoff line.
GRATITUDE_ACK_LINE = "Пожалуйста! Обращайтесь, если будут вопросы."
GRATITUDE_ACK_LINE_EN = "You're welcome! Feel free to reach out anytime."
# Story 12.59 (round-14) - "сколько багги нужно / понадобится / вместит" is a
# capacity question, NOT a price ask ("сколько стоит") or a headcount answer.
_CAPACITY_QUESTION_RE = re.compile(
    r"скольк\w*\b.*\b(нужн\w*|понадоб\w*|надо|потребу\w*|вмест\w*|вмещ\w*"
    r"|помест\w*|хватит)",
    re.IGNORECASE | re.UNICODE | re.DOTALL,
)


def is_capacity_question(question: str) -> bool:
    """A "how many vehicles do we need / will fit" capacity question (round-14,
    Story 12.59) - distinct from a price ask and a plain headcount answer."""
    return bool(_CAPACITY_QUESTION_RE.search(question))


# Story 12.63 (round-15) — derive a buggy-count recommendation from catalog
# capacity when present, else escalate. Headcount from digits or a small set of
# Russian numerals/collectives; seats-per-buggy parsed BUGGY-specifically from
# the RAG so a quadbike figure («2 чел. на квадрике») never leaks in.
_RU_HEADCOUNT_WORDS: dict[str, int] = {
    "один": 1, "одного": 1,
    "два": 2, "две": 2, "двое": 2, "двоих": 2, "вдвоём": 2, "вдвоем": 2,
    "три": 3, "трое": 3, "троих": 3, "втроём": 3, "втроем": 3,
    "четыре": 4, "четверо": 4, "четверых": 4, "вчетвером": 4,
    "пять": 5, "пятеро": 5, "пятерых": 5, "впятером": 5,
    "шесть": 6, "шестеро": 6, "семь": 7, "семеро": 7,
    "восемь": 8, "восьмеро": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12,
}
_BUGGY_CAPACITY_RE = re.compile(
    r"багг\w*[^.\n]{0,40}?(\d+)\s*(?:чел|человек|мест|пассажир)"
    r"|(\d+)\s*(?:чел|человек|мест|пассажир)[^.\n]{0,40}?багг",
    re.IGNORECASE | re.UNICODE,
)


def _parse_headcount(text: str) -> int | None:
    """Headcount from a digit or a Russian numeral/collective («восемь», «двое»)."""
    digits = _parse_count(text)
    if digits is not None:
        return digits
    low = text.lower()
    for word, value in _RU_HEADCOUNT_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            return value
    return None


def _parse_buggy_seats(text: str) -> int | None:
    """Seats-per-buggy from a catalog chunk that states it («Багги — до 4
    человек»). BUGGY-specific (requires «багг» beside the number), so a quadbike
    capacity never matches. ``None`` when the chunk states no buggy capacity."""
    match = _BUGGY_CAPACITY_RE.search(text)
    if match is None:
        return None
    # One alternative matched, so its `\d+` group is a digit string.
    seats = int(match.group(1) or match.group(2))
    return seats if seats > 0 else None


# Story 12.58 (round-14) — an availability INQUIRY («свободно ли?», «занято?»)
# vs a booking REQUEST. An inquiry that doesn't carry a booking-commit verb is
# answered with a plain verdict and never escalates / opens a HITL ticket.
_AVAILABILITY_INQUIRY_RE = re.compile(
    r"свободн\w*|занят\w*|доступн\w*|есть\s+ли\b", re.IGNORECASE | re.UNICODE
)
# Story 12.60 (round-14 R14-1) — fuzzy time windows. An in-scope booking with a
# vague time ("во второй половине дня") gets the day's window checked + a slot
# proposed, never an out-of-scope decline. Ordered specific -> general so
# "второй половине дня" wins over "днём". Hours are clamped by the calendar.
_VAGUE_WINDOW_PATTERNS: tuple[tuple[re.Pattern[str], int, int], ...] = (
    (
        re.compile(r"перв\w*\s+половин\w*\s+дня|до\s+обеда", re.IGNORECASE | re.UNICODE),
        8,
        12,
    ),
    (
        re.compile(
            r"втор\w*\s+половин\w*\s+дня|после\s+обеда", re.IGNORECASE | re.UNICODE
        ),
        12,
        18,
    ),
    (re.compile(r"\bутр", re.IGNORECASE | re.UNICODE), 8, 12),
    (re.compile(r"\bобед", re.IGNORECASE | re.UNICODE), 12, 15),
    (re.compile(r"\bвечер", re.IGNORECASE | re.UNICODE), 16, 20),
    (re.compile(r"\bдн[еёя]м\b", re.IGNORECASE | re.UNICODE), 12, 18),
)


def detect_vague_window(question: str) -> tuple[int, int] | None:
    """Map a fuzzy time phrase to an ``(start_hour, end_hour)`` window, or None."""
    for pattern, start_hour, end_hour in _VAGUE_WINDOW_PATTERNS:
        if pattern.search(question):
            return (start_hour, end_hour)
    return None
_BOOKING_COMMIT_RE = re.compile(
    r"запиш\w*|записа\w*|забронир\w*|бронир\w*|оформ\w*|брон[ьи]\b",
    re.IGNORECASE | re.UNICODE,
)


def is_availability_inquiry(question: str) -> bool:
    """True for a "is this slot free?" question with no booking-commit verb."""
    return bool(_AVAILABILITY_INQUIRY_RE.search(question)) and not _BOOKING_COMMIT_RE.search(
        question
    )


# Story 16 (round-16 R16-4) — gratitude / smalltalk («спасибо большое!»).
_GRATITUDE_RE = re.compile(
    r"спасибо|благодар|\bспс\b|очень\s+помог|вы\s+помог", re.IGNORECASE | re.UNICODE
)
# A decline/closure word alongside the thanks («всё, спасибо», «нет, спасибо»)
# means the customer is finishing — that stays a closure (the operator follows
# up), not a chit-chat ack.
_GRATITUDE_DECLINE_RE = re.compile(
    r"\bнет\b|\bвс[её]\b|не\s+надо|не\s+нужн|отказ", re.IGNORECASE | re.UNICODE
)


def is_gratitude(question: str) -> bool:
    """True for a PURE thank-you turn (thanks with no decline/closure word)."""
    return bool(_GRATITUDE_RE.search(question)) and not _GRATITUDE_DECLINE_RE.search(
        question
    )


# Story 16 (round-16 R16-3) — an eligibility / policy QUESTION («можно ли с
# ребёнком?», «нужны ли права?») — a condition-of-use ask, not a booking. A
# distinctive eligibility noun PLUS a permission/question marker, and no
# booking-commit verb (so «запишите, едем с ребёнком» stays a booking).
_ELIGIBILITY_NOUN_RE = re.compile(
    r"ребён|ребен|\bдет(?:и|ьм|ей|ям|ск)|возраст|беремен|\bсобак|\bживотн"
    r"|\bправа?х?\b|новичк|\bопыт|\bвес\b|инвалид",
    re.IGNORECASE | re.UNICODE,
)
_ELIGIBILITY_MARKER_RE = re.compile(
    r"можн\w*|нужн\w*|допуск\w*|разреш\w*|\bли\b|скольк\w*\s+лет|со\s+скольк",
    re.IGNORECASE | re.UNICODE,
)


def is_eligibility_question(question: str) -> bool:
    """True for an eligibility/policy question (round-16 R16-3)."""
    return (
        bool(_ELIGIBILITY_NOUN_RE.search(question))
        and bool(_ELIGIBILITY_MARKER_RE.search(question))
        and not _BOOKING_COMMIT_RE.search(question)
    )
PRICING_MISS_FALLBACK = "Уточню у коллег и сразу сообщу"
EMPTY_CATALOG_ESCALATION_LINE = "Услуг пока нет. Уточню у коллег и сразу сообщу."
EMPTY_CATALOG_ESCALATION_LINE_EN = (
    "No services are listed yet. I'll check with my colleagues and let you know."
)
# Story 12.05 — appended to the textual reply when a media dispatch failed
# mid-turn so the customer never sees a silent bot.
MATERIAL_DISPATCH_FALLBACK_LINE = (
    "Видео/фото пришлю чуть позже - уточню у коллег."
)
EQUIPMENT_ACK_LINE = "Снаряжение подготовим, расскажу подробнее на месте."
# Story 12.10 — scoping completion (all 5 fields collected). The bot no longer
# asks a filler "wishes" question; it confirms and hands off to a human. When a
# concrete requested time is known and free we say so; when it's busy we offer
# the nearest free slot via the date proposer.
SCOPING_COMPLETE_HANDOFF_LINE = (
    "Спасибо! Передам детали коллегам на подтверждение - вернутся с ответом."
)
SCOPING_COMPLETE_HANDOFF_LINE_EN = (
    "Thank you! I'll pass the details to my colleagues for confirmation - "
    "they'll get back to you."
)
SLOT_FREE_HANDOFF_LINE = (
    "Спасибо! Это время свободно - передам коллегам для подтверждения."
)
SLOT_FREE_HANDOFF_LINE_EN = (
    "Thank you! That time is free - I'll pass it to my colleagues for "
    "confirmation."
)
# Story 12.58 (round-14) — a customer ASKING whether a slot is free («свободно
# ли в 16:30?») gets a plain text verdict, NOT a booking handoff/HITL ticket.
SLOT_FREE_INQUIRY_LINE = "Да, это время свободно."
SLOT_FREE_INQUIRY_LINE_EN = "Yes, that time is free."
# Story 12.60 (round-14 R14-1) — a vague time window («во второй половине дня»)
# is answered by proposing a concrete free slot in that window and asking the
# customer to confirm or name a time, NEVER an out-of-scope decline. ``{time}``
# is a free slot the calendar returned.
VAGUE_WINDOW_OFFER_LINE = (
    "Да, есть свободное время, например в {time}. "
    "Подойдёт или назовите удобное время?"
)
VAGUE_WINDOW_OFFER_LINE_EN = (
    "Yes, there's free time then - for example {time}. "
    "Does that work, or let me know a time that suits you?"
)
# Story 12.32 (D1) — the customer named a concrete time but the calendar could
# NOT be checked. Say plainly we'll verify it; never claim it's free
# (SLOT_FREE_HANDOFF_LINE) and never the plain accept-shaped completion line —
# both would imply a slot we never confirmed.
SLOT_UNVERIFIED_HANDOFF_LINE = (
    "Спасибо! Проверю это время и вернусь к вам с ответом."
)
SLOT_UNVERIFIED_HANDOFF_LINE_EN = (
    "Thank you! I'll check that time and get back to you with an answer."
)
SLOT_BUSY_LINE = "К сожалению, это время уже занято."
SLOT_BUSY_LINE_EN = "Unfortunately, that time is already taken."
# Story 12.29 — the availability engine already distinguishes *why* a slot is
# unavailable; surface that as distinct customer copy instead of mislabeling
# every unavailable slot "занято" (busy). A 23:00 request is outside working
# hours, not booked by someone else. Unmapped reasons (plain `busy`, the rare
# `outside_lookahead`, or `None`) keep SLOT_BUSY_LINE as the safe default.
SLOT_OFF_HOURS_LINE = "К сожалению, это время вне рабочих часов."
SLOT_WRONG_DAY_LINE = "К сожалению, в этот день услуга недоступна."
SLOT_CLOSED_DATE_LINE = "К сожалению, в этот день мы не работаем."
SLOT_IN_PAST_LINE = "К сожалению, это время уже прошло."
SLOT_OFF_HOURS_LINE_EN = "Unfortunately, that time is outside our working hours."
SLOT_WRONG_DAY_LINE_EN = "Unfortunately, the service isn't available that day."
SLOT_CLOSED_DATE_LINE_EN = "Unfortunately, we're closed that day."
SLOT_IN_PAST_LINE_EN = "Unfortunately, that time has already passed."
_UNAVAILABLE_LEAD_LINES: dict[str, str] = {
    REASON_OUTSIDE_WORKING_HOURS: SLOT_OFF_HOURS_LINE,
    REASON_WRONG_SERVICE_DAY: SLOT_WRONG_DAY_LINE,
    REASON_DATE_EXCEPTION: SLOT_CLOSED_DATE_LINE,
    REASON_IN_PAST: SLOT_IN_PAST_LINE,
}
_UNAVAILABLE_LEAD_LINES_EN: dict[str, str] = {
    REASON_OUTSIDE_WORKING_HOURS: SLOT_OFF_HOURS_LINE_EN,
    REASON_WRONG_SERVICE_DAY: SLOT_WRONG_DAY_LINE_EN,
    REASON_DATE_EXCEPTION: SLOT_CLOSED_DATE_LINE_EN,
    REASON_IN_PAST: SLOT_IN_PAST_LINE_EN,
}
# Story 12.11 - when the customer declines the field just asked ("не нужно",
# "0", "без водителей"), record this sentinel so the funnel advances instead of
# re-asking forever. Non-None → satisfies completeness; reads cleanly in the
# operator's booking summary ("drivers: не требуется").
SCOPING_DECLINED_SENTINEL = "не требуется"
# Story 12.15 — the deterministic fallback questions and the numeric-field set
# now come from the active `ScopingSchema` (`question_for` / `numeric_keys`), so
# a per-service anketa drives them instead of these hardcoded transfer fields.
# Story 12.10 — asked when scoping is complete but no concrete date+time was
# given AND the calendar can check it. We ask for date+time TOGETHER (not just
# the time) because ``intent_merge`` REPLACES the ``dates`` field — a bare
# follow-up time would otherwise drop a previously-collected date.
ASK_FOR_TIME_LINE = (
    "Уточните, пожалуйста, желаемые дату и время - проверю по календарю "
    "и подтвержу."
)
# Story 12.47 (round-10 N3) — English variants of the deterministic
# customer-facing lines, selected per turn via ``localize(.., ctx.language)``.
ASK_FOR_TIME_LINE_EN = (
    "Could you let me know the desired date and time? "
    "I'll check the calendar and confirm."
)
# Story 12.62 (round-15) — when the DATE is already known, ask only for the
# time (the date is re-attached deterministically on the reply, so it isn't
# lost to the intent_merge replace).
ASK_FOR_TIME_ONLY_LINE = (
    "Уточните, пожалуйста, желаемое время - проверю по календарю и подтвержу."
)
ASK_FOR_TIME_ONLY_LINE_EN = (
    "Could you let me know the desired time? I'll check the calendar and confirm."
)
# Story 12.19 — acknowledgement when the customer accepts the offered
# alternative slot in pitching ("да" / "давайте на 31-ое в 8"). Names the slot
# but frames it as passed to the operator *for* confirmation — never as already
# confirmed (the colleague has not confirmed yet); the operator (already
# escalated) books it. ``{day_month}`` = "31 мая", ``{time}`` = "08:00".
PITCHING_ACCEPT_CONFIRM_LINE = (
    "Отлично, передаю детали коллеге на подтверждение - {day_month} на {time}."
)
PITCHING_ACCEPT_CONFIRM_LINE_EN = (
    "Great - I'm passing the details to a colleague for confirmation: "
    "{day_month} at {time}."
)

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
        STAGE_PITCHING,
        STAGE_PRICING,
        STAGE_AWAITING_OPERATOR_PRICE,
        STAGE_PROPOSING,
        STAGE_CLOSING,
        STAGE_AWAITING_TIME,
    }
)
# Stages where mid-funnel asides (catalog / concept / price) are intercepted
# before the stage handler. Greeting is excluded: a brand-new chat hits the
# greeting branch first and the sales-intent gate already covers catalog
# phrases.
_ASIDE_INTERCEPT_STAGES: frozenset[str] = frozenset(
    {STAGE_SCOPING, STAGE_PITCHING}
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
# Story 12.47 (round-10 N3) - English month names so an English thread's
# busy-alternative tail / accept confirmation names the slot in English too.
_MONTHS_EN: dict[int, str] = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}

# Story 12.33 (D9) — Monday..Sunday, indexed by datetime.weekday().
_WEEKDAYS_RU: tuple[str, ...] = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
# The scoping/greeting LLM gets no current-date context otherwise, so it
# resolves a weekday ("в понедельник") to a guessed absolute date. Anchor it on
# the project-local today. Defaults to the platform timezone (Europe/Moscow,
# matching settings.default_timezone); a far-off-tz project near local midnight
# could see a 1-day-off label — the deterministic resolver still uses the
# calendar's own tz, so the verdict is unaffected.
_PROMPT_TODAY_TZ = ZoneInfo("Europe/Moscow")


def _format_today_ru(now: datetime, tz: ZoneInfo = _PROMPT_TODAY_TZ) -> str:
    """Russian 'сегодня' label ("1 июня 2026 года, понедельник") for the date
    context injected into the greeting/scoping prompts (Story 12.33 / D9)."""
    local = now.astimezone(tz)
    return (
        f"{local.day} {_MONTHS_GENITIVE[local.month]} {local.year} года, "
        f"{_WEEKDAYS_RU[local.weekday()]}"
    )


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


@dataclass(frozen=True)
class _CalBookingCtx:
    """Resolved "this booking is calendar-actionable" context.

    Present only when the project has calendar enabled, a settings row, and
    EXACTLY ONE active service to anchor the slot duration — the precise
    precondition shared by ``_check_requested_slot`` (does the requested time
    fit?) and ``_should_ask_for_time`` (no time given — worth asking?).
    """

    project_id: int
    settings: Any
    project_tz: ZoneInfo
    service_rule: Any


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


class _CalendarSettingsRepo(Protocol):
    def is_enabled(self, project_id: int) -> bool: ...

    def get(self, project_id: int) -> Any | None: ...

    def list_service_rules(self, project_id: int) -> list[Any]: ...


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


def _format_missing_fields(intent: Intent, schema: ScopingSchema) -> str:
    """Missing required fields, each annotated with its question so the LLM
    knows what a bare key like ``topic`` means for a custom anketa."""
    missing = intent.missing_fields(schema.required_keys())
    if not missing:
        return "(все собраны)"
    return "\n".join(f"- {key}: {schema.question_for(key)}" for key in missing)


def _format_intent_summary(intent: Intent) -> str:
    """One-line booking summary for the operator HITL DM (escalation_context)."""
    parts = [
        f"{name}={value}"
        for name, value in intent.to_dict().items()
        if value is not None
    ]
    body = "; ".join(parts) if parts else "нет деталей"
    return f"бронь: {body}"


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


def _build_greeting_prompt(*, today: str) -> str:
    # The greeting no longer states a name, so the persona is not interpolated
    # here — ``.format()`` resolves ``{today}`` (Story 12.33) + the escaped
    # JSON braces.
    return _GREETING_PROMPT_TEMPLATE.format(today=today)


# Story 12.55 (round-12) — appended to the greeting prompt when the customer is
# ALREADY in conversation (a mid-thread intent switch, or a fresh booking after
# a handoff), so the persona doesn't re-open with "Здравствуйте". Appended at
# the call site like reply_language_directive; the LLM still answers on-topic.
_RETURNING_NO_GREETING_DIRECTIVE = (
    "\n\nВАЖНО: клиент уже в этом диалоге с вами - НЕ здоровайтесь повторно "
    "(без «Здравствуйте», «Привет», «Добрый день»), отвечайте сразу по существу."
)
# Story 12.56 (round-13) - the directive above is "soft": the LLM sometimes
# greets anyway. On a returning turn we DETERMINISTICALLY strip a leading
# salutation from the reply so a re-greeting can't slip through.
_LEADING_GREETING_RE = re.compile(
    r"^\s*(?:здравствуй(?:те)?|здрасте|привет(?:ствую)?|здаров[ао]"
    r"|добрый\s+(?:день|вечер)|доброе\s+утро|доброго\s+(?:дня|вечера|утра)"
    r"|доброй\s+ночи)[\s,.!…—–-]*",
    re.IGNORECASE | re.UNICODE,
)


def _format_alternative_tail(alternative: datetime, language: str) -> str:
    """The localized " Ближайшее свободное время - <day month>, <HH:MM>." tail.
    Shared by the busy handoff and the availability-inquiry verdict (Story
    12.58). Uses a plain hyphen per Story 12.57 (round-14)."""
    if language == "en":
        return (
            f" The nearest available time is "
            f"{_MONTHS_EN[alternative.month]} {alternative.day}, "
            f"{alternative.strftime('%H:%M')}."
        )
    return (
        f" Ближайшее свободное время - {alternative.day} "
        f"{_MONTHS_GENITIVE[alternative.month]}, "
        f"{alternative.strftime('%H:%M')}."
    )


def _strip_leading_greeting(text: str) -> str:
    """Drop a leading Russian salutation (and its trailing punctuation) so a
    returning-customer reply doesn't open with "Здравствуйте" (Story 12.56).
    Returns the original when there's no greeting, or when nothing would remain
    (a greeting-only reply is kept rather than emptied)."""
    stripped = _LEADING_GREETING_RE.sub("", text, count=1).lstrip()
    if not stripped:
        return text
    return stripped[0].upper() + stripped[1:]


def _parse_count(text: str) -> int | None:
    """Story 12.14 - the count in a terse reply ("1" → 1, "троих" → None).

    Only digits are bound here; "0" is caught upstream by the decline path, and
    word-numerals stay the LLM's job (Layer A). Returns ``None`` when the reply
    carries no digit so the caller leaves the field unbound.
    """
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def _format_pending_instruction(
    intent: Intent, required: tuple[str, ...] | None = None
) -> str:
    """Story 12.14 - name the field the customer is answering this turn.

    The stateless extractor otherwise sees only the bare reply ("1") and, told
    not to invent values, drops it — so the funnel re-asks the same field
    forever. Naming the top-missing field lets the LLM bind a terse value.
    Empty string when nothing is missing (the awaiting-time follow-up reuses
    this prompt with a complete intent).
    """
    missing = intent.missing_fields(required)
    if not missing:
        return ""
    pending = missing[0]
    return (
        f"Клиент сейчас отвечает на вопрос о поле «{pending}». Если его реплика - "
        f"это значение для этого поля (например, просто число «1» или «2»), "
        f'обязательно запиши его в extracted_fields["{pending}"].'
    )


def _format_extracted_fields_spec(schema: ScopingSchema) -> str:
    """Generate the ``extracted_fields`` JSON keys block from the schema, so the
    LLM extracts exactly the active anketa's fields (number vs string typed)."""
    lines = []
    for field in schema.fields:
        placeholder = (
            "<число или опусти ключ>"
            if field.kind == "number"
            else '"<строка или опусти ключ>"'
        )
        lines.append(f'    "{field.key}": {placeholder}')
    return ",\n".join(lines)


def _build_scoping_prompt(
    *, persona: str, intent: Intent, schema: ScopingSchema, today: str
) -> str:
    return _SCOPING_PROMPT_TEMPLATE.format(
        persona=persona,
        today=today,
        known_fields=_format_known_fields(intent),
        missing_fields=_format_missing_fields(intent, schema),
        pending_instruction=_format_pending_instruction(
            intent, schema.required_keys()
        ),
        extracted_fields_spec=_format_extracted_fields_spec(schema),
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
        calendar_settings_repo: _CalendarSettingsRepo | None = None,
        calendar_token_provider: Any | None = None,
        calendar_freebusy_client: Any | None = None,
        operator_chat_resolver: Callable[[str], int | None] | None = None,
        scoping_required_fields_getter: Callable[[], tuple[str, ...]] | None = None,
        scoping_schema_getter: Callable[[AnswerContext], ScopingSchema] | None = None,
    ) -> None:
        self._required_fields_getter = scoping_required_fields_getter
        self._schema_getter = scoping_schema_getter
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
        self._calendar_settings_repo = calendar_settings_repo
        self._calendar_token_provider = calendar_token_provider
        self._calendar_freebusy_client = calendar_freebusy_client
        self._operator_chat_resolver = operator_chat_resolver

    def _required_fields(self) -> tuple[str, ...]:
        """The scoping fields that must be collected (Story 12.12).

        Resolved per turn from the injected getter (runtime-config driven);
        falls back to all five when unset, and to all five if the getter ever
        returns an empty tuple, so the funnel never has nothing to ask.
        """
        if self._required_fields_getter is None:
            return _FIELD_NAMES
        return tuple(self._required_fields_getter()) or _FIELD_NAMES

    def _resolve_schema(self, ctx: AnswerContext) -> ScopingSchema:
        """The anketa for this turn (Story 12.15).

        A per-service schema getter wins when wired (Story 12.16); otherwise the
        built-in transfer schema, narrowed to the legacy ``required`` subset so
        the existing ``scoping_required_fields`` config keeps working unchanged.
        """
        if self._schema_getter is not None:
            return self._schema_getter(ctx)
        return TRANSFER_SCHEMA.with_required(self._required_fields())

    async def try_answer(
        self, *, question: str, ctx: AnswerContext
    ) -> AnswerResult:
        # Story 12.47 (round-10 N3) - pin the turn's language ONCE from the
        # customer's current message, so every downstream line (LLM-generated
        # AND the deterministic constants below) mirrors it. Detection is the
        # same cheap heuristic the LLM directive uses; an English thread now
        # stays English on every turn instead of reverting to Russian the
        # moment a fixed constant (ask-for-time, busy/free verdict) fires.
        ctx = replace(ctx, language=detect_language(question))
        result = await self._dispatch(question=question, ctx=ctx)
        # Story 12.54 (round-12 D5) — a message mixing a booking with an
        # out-of-scope ask is handled by the funnel (the booking part), which
        # silently dropped the off-topic ask. Append a one-line decline (both
        # intents present → genuinely mixed; a pure booking never matches
        # is_out_of_scope, so this never fires on a normal booking).
        if (
            result.handled
            and result.text
            and is_out_of_scope(question, normalizer=self._normalizer)
            and is_sales_intent(question, normalizer=self._normalizer)
        ):
            result = replace(
                result,
                text=result.text
                + "\n"
                + localize(
                    MIXED_OUT_OF_SCOPE_SUFFIX,
                    MIXED_OUT_OF_SCOPE_SUFFIX_EN,
                    language=ctx.language,
                ),
            )
        # Story 12.27 — a cancellation turn suppresses the +24h nudge so the
        # customer isn't asked "still thinking about booking?" a day after asking
        # to cancel. Any other handled turn schedules one nudge as before.
        if (
            result.handled
            and ctx.chat_id is not None
            and not result.metadata.get("suppress_followup")
        ):
            await self._enqueue_followup(ctx=ctx)
        return result

    async def _dispatch(
        self, *, question: str, ctx: AnswerContext
    ) -> AnswerResult:
        if ctx.chat_id is None:
            return _skip("no_chat_id")

        state = await asyncio.to_thread(self._state_repo.get, ctx.chat_id)

        # Story 12.27 — a cancellation request is its own intent. Catch it before
        # the booking funnel (whose бронь/запись seeds would pull "хочу отменить
        # запись" into scoping) and before the not-sales-intent skip (so a bare
        # "можно отменить?" routes to a human instead of falling through to
        # generic RAG/HITL). Fires in any state/stage.
        if is_cancellation(question, normalizer=self._normalizer):
            return await self._handle_cancellation(
                question=question, ctx=ctx, state=state
            )

        # Story 12.34 — an out-of-scope ask (a restaurant / hotel) must be
        # politely declined, not accepted as a booking. The polite-decline
        # ScopeGuardAnswerer runs LAST in the pipeline, so in an active funnel
        # `_handle_scoping`/`_handle_pitching` would otherwise claim the turn and
        # emit the booking-acceptance line. Gated on ``not is_sales_intent`` so a
        # mixed "хочу багги … и ресторан?" stays in the funnel and a field answer
        # is never swallowed; fires in any stage and leaves funnel state intact.
        if is_out_of_scope(
            question, normalizer=self._normalizer
        ) and not is_sales_intent(question, normalizer=self._normalizer):
            return self._handle_out_of_scope(ctx=ctx)

        # Story 12.59 (round-14) - a capacity question ("сколько багги нужно?")
        # is not a booking; the bot has no per-vehicle capacity data, so answer
        # it by escalating to a human with checking-options copy — never thank
        # and hand off the mis-read booking. Fires in any state.
        if is_capacity_question(question):
            return await self._handle_capacity_question(question=question, ctx=ctx)

        # Story 16 (round-16 R16-1) — an impossible calendar date («31 июня»,
        # «31.06») is rejected with a clarify, never accepted as a booking. Fires
        # before the funnel so the bad date never reaches the slot check.
        if names_invalid_date(
            question, now=self._clock(), project_tz=ZoneInfo(ctx.timezone)
        ):
            return self._handle_invalid_date(ctx=ctx)

        # Story 16 (round-16 R16-4) — gratitude / smalltalk gets a courteous ack,
        # not a booking-handoff. Gated on not-sales so «спасибо, запишите» books.
        if is_gratitude(question) and not is_sales_intent(
            question, normalizer=self._normalizer
        ):
            return self._handle_gratitude(ctx=ctx)

        # Story 16 (round-16 R16-3) — an eligibility/policy question («можно с
        # ребёнком?») is answered as a QUESTION: RAG-grounded if the catalog has
        # the policy, else deferred to a human — never a booking handoff.
        if is_eligibility_question(question):
            return await self._answer_concept_via_rag(
                term=question,
                ctx=ctx,
                current_stage=str(state.get("current_stage") if state else STAGE_NEW),
            )

        if state is None:
            if not is_sales_intent(question, normalizer=self._normalizer):
                return _skip("not_sales_intent")
            return await self._handle_greeting(question=question, ctx=ctx)

        current_stage = str(state.get("current_stage") or STAGE_NEW)
        if current_stage == STAGE_DORMANT:
            if not is_sales_intent(question, normalizer=self._normalizer):
                return _skip("not_sales_intent")
            # Story 12.55 — re-engaging after dormancy is mid-thread, not first
            # contact: don't greet again.
            return await self._handle_greeting(
                question=question, ctx=ctx, returning=True
            )

        # Story 12.06 — intercept mid-funnel conversational asides BEFORE the
        # deferred-stage skip so a pitching/pricing customer can still ask
        # "что у вас есть?" or "что такое X?" without losing funnel state.
        if current_stage in _ASIDE_INTERCEPT_STAGES:
            aside = await self._maybe_handle_aside(
                question=question, ctx=ctx, state=state
            )
            if aside is not None:
                return aside

        if current_stage in (STAGE_PRICING, STAGE_AWAITING_OPERATOR_PRICE):
            # Story 12.46 (round-9 R9-1) — a price ask is a one-shot aside; it must
            # NOT trap the customer in pricing. When the customer has moved on to a
            # fresh greeting or a booking, re-enter the funnel from greeting so the
            # turn is handled on its own merits — never answered with a stale price
            # line. A price follow-up ("ну так сколько в итоге?") stays in pricing.
            # (Cancellation / out-of-scope were already routed above.)
            if self._has_moved_on_from_pricing(question=question, ctx=ctx):
                return await self._handle_greeting(
                    question=question, ctx=ctx, returning=True
                )
            return await self._handle_pricing(
                question=question, ctx=ctx, state=state
            )

        if current_stage == STAGE_SCOPING:
            return await self._handle_scoping(
                question=question, ctx=ctx, state=state
            )

        if current_stage == STAGE_PITCHING:
            return await self._handle_pitching(
                question=question, ctx=ctx, state=state
            )

        if current_stage == STAGE_AWAITING_TIME:
            return await self._handle_awaiting_time(
                question=question, ctx=ctx, state=state
            )

        if current_stage == STAGE_PROPOSING:
            if self._date_proposer is None:
                return _skip("stage_not_implemented_yet")
            return await self._handle_proposing(
                question=question, ctx=ctx, state=state
            )

        if current_stage == STAGE_CLOSING:
            # Story 12.23 — a returning customer whose prior booking was already
            # handed off (terminal ``closing``) and who now sends a FRESH sales
            # intent restarts the funnel from greeting; ``_handle_greeting`` builds
            # a clean ``Intent`` and (via ``_persist`` with no ``last_proposal``)
            # clears the stale offered slot. A non-sales reply ("спасибо") keeps
            # the sticky handoff — the human still owns the conversation.
            if is_sales_intent(question, normalizer=self._normalizer):
                # Story 12.55 — a fresh booking after a handoff is mid-thread:
                # restart the funnel but don't re-greet.
                return await self._handle_greeting(
                    question=question, ctx=ctx, returning=True
                )
            return await self._handle_closing(
                question=question, ctx=ctx, state=state
            )

        if current_stage == STAGE_NEW:
            return await self._handle_greeting(question=question, ctx=ctx)

        # Unknown / future stage value — defer to downstream answerers.
        return _skip("stage_not_implemented_yet")

    def _has_moved_on_from_pricing(
        self, *, question: str, ctx: AnswerContext
    ) -> bool:
        """True when a pricing-stage turn is a fresh greeting or a booking, so the
        funnel should re-engage (Story 12.46, round-9 R9-1). A price follow-up
        ("ну так сколько в итоге?") returns False and stays in pricing.

        The tz only gates the parses-as-a-booking check; the downstream busy check
        re-parses with the real project tz.
        """
        if _GREETING_RE.search(question):
            return True
        now = ctx.now
        tz = ZoneInfo(ctx.timezone)
        return (
            now is not None
            and now.tzinfo is not None
            and extract_requested_start(text=question, now=now, project_tz=tz)
            is not None
        )

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
        self, *, question: str, ctx: AnswerContext, returning: bool = False
    ) -> AnswerResult:
        # Story 12.45 (round-8 N3) - mirror the customer's language. Empty suffix
        # for Russian (the default), so RU prompts are unchanged.
        system = _build_greeting_prompt(
            today=_format_today_ru(self._clock())
        ) + reply_language_directive(question)
        # Story 12.55/12.56 — on a mid-thread re-entry (returning after a handoff
        # / an intent switch) the bot shouldn't open with "Здравствуйте" - UNLESS
        # the customer greeted first, in which case greeting back is natural.
        suppress_greeting = (
            returning and _LEADING_GREETING_RE.match(question) is None
        )
        if suppress_greeting:
            system += _RETURNING_NO_GREETING_DIRECTIVE
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
        # Story 12.43 (round-8 N1) — the greeting LLM sometimes stores a numeric
        # date WITHOUT its co-located time ("03.06"), which extract_requested_start
        # can't parse, so the busy check below silently skipped and the slot was
        # handed off unchecked. The raw message always carries date+time.
        merged = self._dates_with_raw_fallback(
            intent=merged, question=question, ctx=ctx
        )
        # Story 16 (round-16 R16-2) — «<день1> или <день2> в HH:MM» → an explicit
        # per-day verdict, so the confirmed day isn't left unstated.
        multi_date = await self._maybe_answer_multi_date(
            question=question, ctx=ctx, stage_before=STAGE_NEW
        )
        if multi_date is not None:
            return multi_date
        # Story 12.58 (round-14) — a pure availability INQUIRY («…в 16:30
        # свободно?») gets a plain verdict, never a booking handoff/HITL. Checked
        # before the busy-intercept so an inquiry isn't turned into a booking.
        inquiry = await self._maybe_answer_availability_inquiry(
            question=question, ctx=ctx, merged_intent=merged
        )
        if inquiry is not None:
            return inquiry
        # Story 12.60 (round-14 R14-1) — a vague time («во второй половине дня»)
        # gets the window checked + a concrete slot proposed, never a decline.
        vague = await self._maybe_answer_vague_window(
            question=question, ctx=ctx, merged_intent=merged, stage_before=STAGE_NEW
        )
        if vague is not None:
            return vague
        # Story 12.25 — if the opener carries a concrete date+time and the
        # slot is already busy, surface it now: customers should not be
        # asked logistics questions about a slot that was never going to
        # happen at that time. Falls through (returns ``None``) on free /
        # not-connected / error / calendar-disabled / multi-service.
        intercept = await self._maybe_intercept_busy_slot(
            ctx=ctx,
            existing_intent=Intent(),
            merged_intent=merged,
            stage_before=STAGE_NEW,
        )
        if intercept is not None:
            return intercept
        # Story 12.28 - a first-contact opener that asks a price ("8 человек,
        # сколько стоит?") must be answered, not dropped. ``classify_turn``
        # already tags it a price ask; route to pricing BEFORE the funnel
        # advances to asking for a date. The fields the greeting LLM just
        # extracted (e.g. headcount) ride along so they inform the quote and
        # are not re-asked. Falls through (``None``) on a non-price opener or
        # when pricing isn't configured.
        price_intercept = await self._maybe_intercept_price_ask(
            question=question, ctx=ctx, merged=merged
        )
        if price_intercept is not None:
            return price_intercept
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
        # Story 12.56 — deterministically strip a leading salutation the LLM may
        # have added despite the directive (only when we're suppressing).
        reply_text = (
            _strip_leading_greeting(next_question)
            if suppress_greeting
            else next_question
        )
        return AnswerResult(
            handled=True,
            text=reply_text,
            metadata={
                "answerer": NAME,
                "stage_before": STAGE_NEW,
                "stage_after": stage_after,
            },
        )

    def _handle_out_of_scope(self, *, ctx: AnswerContext) -> AnswerResult:
        """Story 12.34 (D7) - politely decline an out-of-scope ask and redirect.

        A plain handled reply: no HITL escalation, and no ``_persist`` — the
        booking funnel (if any) is left exactly as it was so the customer
        resumes on their next on-topic turn. ``suppress_followup`` so an
        off-topic aside doesn't schedule a "still thinking about booking?" nudge.
        """
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "sales_turn_kind": "out_of_scope_decline",
            },
        )
        return AnswerResult(
            handled=True,
            text=localize(
                OUT_OF_SCOPE_DECLINE_LINE,
                OUT_OF_SCOPE_DECLINE_LINE_EN,
                language=ctx.language,
            ),
            metadata={
                "answerer": NAME,
                "sales_turn_kind": "out_of_scope_decline",
                "suppress_followup": True,
            },
        )

    def _handle_invalid_date(self, *, ctx: AnswerContext) -> AnswerResult:
        """Story 16 (round-16 R16-1) — reject an impossible date with a clarify;
        funnel state left intact, no escalation."""
        logger.info(
            "sales_answerer_handled",
            extra={"trace_id": ctx.trace_id, "sales_turn_kind": "invalid_date"},
        )
        return AnswerResult(
            handled=True,
            text=localize(
                INVALID_DATE_CLARIFY_LINE,
                INVALID_DATE_CLARIFY_LINE_EN,
                language=ctx.language,
            ),
            metadata={
                "answerer": NAME,
                "sales_turn_kind": "invalid_date",
                "suppress_followup": True,
            },
        )

    def _handle_gratitude(self, *, ctx: AnswerContext) -> AnswerResult:
        """Story 16 (round-16 R16-4) — courteous ack for a thank-you; never a
        booking-handoff line. No ``_persist`` (funnel left intact)."""
        logger.info(
            "sales_answerer_handled",
            extra={"trace_id": ctx.trace_id, "sales_turn_kind": "gratitude"},
        )
        return AnswerResult(
            handled=True,
            text=localize(
                GRATITUDE_ACK_LINE, GRATITUDE_ACK_LINE_EN, language=ctx.language
            ),
            metadata={
                "answerer": NAME,
                "sales_turn_kind": "gratitude",
                "suppress_followup": True,
            },
        )

    async def _buggy_seats_from_catalog(self, *, ctx: AnswerContext) -> int | None:
        """Seats-per-buggy from the RAG catalog when it states it, else ``None``
        (Story 12.63). Best-effort + BUGGY-specific — today the live catalog
        carries no buggy capacity, so this returns ``None`` and the caller
        escalates; it auto-derives once a «Багги — до N человек» line exists."""
        if self._rag is None:
            return None
        chunks = await asyncio.to_thread(
            self._rag.retrieve,
            query="сколько человек вмещает багги вместимость мест",
            limit=5,
            project_id=ctx.project_id,
        )
        for chunk in chunks:
            seats = _parse_buggy_seats(chunk.chunk_text)
            if seats is not None:
                return seats
        return None

    async def _derive_buggy_count(
        self, *, question: str, ctx: AnswerContext
    ) -> tuple[int, int] | None:
        """``(headcount, buggy_count)`` derived from the question's headcount and
        the catalog's seats-per-buggy, or ``None`` when either is unavailable."""
        headcount = _parse_headcount(question)
        if headcount is None or headcount <= 0:
            return None
        seats = await self._buggy_seats_from_catalog(ctx=ctx)
        if seats is None:
            return None
        return headcount, (headcount + seats - 1) // seats  # ceil division

    async def _handle_capacity_question(
        self, *, question: str, ctx: AnswerContext
    ) -> AnswerResult:
        """Story 12.59/12.63 — answer a capacity question. Derive a buggy-count
        recommendation from the catalog when the data is there; otherwise
        escalate to a human with checking-options copy (never the booking
        thank-you). No ``_persist`` — the funnel (if any) is left intact.
        """
        derived = await self._derive_buggy_count(question=question, ctx=ctx)
        if derived is not None:
            headcount, count = derived
            logger.info(
                "sales_answerer_handled",
                extra={
                    "trace_id": ctx.trace_id,
                    "sales_turn_kind": "capacity_answered",
                    "headcount": headcount,
                    "buggy_count": count,
                },
            )
            return AnswerResult(
                handled=True,
                text=localize(
                    f"На {headcount} человек понадобится примерно {count} багги.",
                    f"For {headcount} people you'd need about {count} buggies.",
                    language=ctx.language,
                ),
                metadata={
                    "answerer": NAME,
                    "sales_turn_kind": "capacity_answered",
                    "suppress_followup": True,
                },
            )
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "sales_turn_kind": "capacity_question",
                "hitl_reason": HITL_REASON_CAPACITY,
            },
        )
        return AnswerResult(
            handled=True,
            text=localize(
                CAPACITY_ESCALATION_LINE,
                CAPACITY_ESCALATION_LINE_EN,
                language=ctx.language,
            ),
            response_mode=RESPONSE_MODE_SALES_ESCALATION,
            metadata={
                "answerer": NAME,
                "sales_turn_kind": "capacity_question",
                "escalate": True,
                "hitl_reason": HITL_REASON_CAPACITY,
                "escalation_context": f"Вопрос о вместимости: {question}",
            },
        )

    async def _handle_cancellation(
        self,
        *,
        question: str,
        ctx: AnswerContext,
        state: dict[str, Any] | None,
    ) -> AnswerResult:
        """Route a cancellation request to a human (Story 12.27).

        No booking-of-record exists to cancel autonomously (operators finalize
        bookings), so we acknowledge with a cancellation-specific line and open a
        HITL ticket tagged ``sales_cancellation_request`` carrying the customer's
        verbatim message (via ``escalation_context``). The funnel is parked at
        the terminal ``closing`` stage — a human owns it now; a later fresh
        booking intent re-greets via Story 12.23. The +24h nudge is suppressed
        for this turn (the inbound route already cancelled any pending one when
        the customer replied).
        """
        intent = Intent.from_dict((state or {}).get("collected_intent") or {})
        stage_before = str((state or {}).get("current_stage") or STAGE_NEW)
        await self._persist(
            ctx=ctx, current_stage=STAGE_CLOSING, intent=intent
        )
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": stage_before,
                "stage_after": STAGE_CLOSING,
                "sales_turn_kind": "cancellation_request",
                "hitl_reason": HITL_REASON_CANCELLATION,
            },
        )
        return AnswerResult(
            handled=True,
            text=localize(
                CANCELLATION_HANDOFF_LINE,
                CANCELLATION_HANDOFF_LINE_EN,
                language=ctx.language,
            ),
            response_mode=RESPONSE_MODE_SALES_ESCALATION,
            metadata={
                "answerer": NAME,
                "stage_before": stage_before,
                "stage_after": STAGE_CLOSING,
                "sales_turn_kind": "cancellation_request",
                "escalate": True,
                "hitl_reason": HITL_REASON_CANCELLATION,
                "escalation_context": CANCELLATION_ESCALATION_CONTEXT,
                "suppress_followup": True,
            },
        )

    async def _extract_and_merge(
        self,
        *,
        question: str,
        ctx: AnswerContext,
        state: dict[str, Any],
        stage_label: str,
        schema: ScopingSchema,
    ) -> tuple[Intent, str, dict[str, Any]] | AnswerResult:
        """Run the scoping LLM extraction and merge it into the stored intent.

        Returns ``(merged_intent, next_question, extracted_fields)`` on success,
        or a ``_skip`` ``AnswerResult`` when the LLM response is unusable (schema
        violation / transport error) so the caller falls through. Shared by the
        scoping turn and the awaiting-time follow-up — both re-extract the
        customer's latest message identically.
        """
        persona = self._persona_getter()
        existing_intent = Intent.from_dict(state.get("collected_intent") or {})
        system = _build_scoping_prompt(
            persona=persona,
            intent=existing_intent,
            schema=schema,
            today=_format_today_ru(self._clock()),
        ) + reply_language_directive(question)  # N3 - mirror the customer's language
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
                    "stage": stage_label,
                    "reason": str(exc),
                },
            )
            return _skip("llm_schema_violation")
        except Exception as exc:  # defensive — LLM transport failure
            logger.warning(
                "sales_llm_transport_error",
                extra={
                    "trace_id": ctx.trace_id,
                    "stage": stage_label,
                    "error": repr(exc),
                },
            )
            return _skip("llm_transport_error")
        merged = intent_merge(existing_intent, extracted, allowed=schema.keys())
        # Story 12.43 (round-8 N1) — see _handle_greeting: fall back to the raw
        # message for `dates` when the LLM stored a numeric date time-less.
        merged = self._dates_with_raw_fallback(
            intent=merged, question=question, ctx=ctx
        )
        return (merged, next_question, extracted)

    async def _handle_scoping(
        self,
        *,
        question: str,
        ctx: AnswerContext,
        state: dict[str, Any],
    ) -> AnswerResult:
        schema = self._resolve_schema(ctx)
        required = schema.required_keys()
        # Story 12.14 — the field we asked last turn is the top-missing field of
        # the intent we resume with. Captured BEFORE the merge so we can tell
        # whether the customer's reply actually advanced it.
        existing = Intent.from_dict(state.get("collected_intent") or {})
        pre_missing = existing.missing_fields(required)
        pending_field = pre_missing[0] if pre_missing else None
        outcome = await self._extract_and_merge(
            question=question,
            ctx=ctx,
            state=state,
            stage_label=STAGE_SCOPING,
            schema=schema,
        )
        if isinstance(outcome, AnswerResult):
            return outcome
        merged, next_question, extracted = outcome
        # Story 12.11 - the customer declined the field just asked ("не нужно",
        # "0", "без водителей"). The declined field is the topmost-missing one
        # (a decline fills nothing), so record a sentinel for it and advance
        # instead of re-asking forever. If that completes scoping we fall
        # through to the completion path; otherwise we ask the NEXT field with a
        # deterministic question (the LLM's was for the field we just filled).
        if not merged.is_complete(required) and is_decline(
            question, normalizer=self._normalizer
        ):
            declined_field = merged.missing_fields(required)[0]
            merged = merged.with_field(declined_field, SCOPING_DECLINED_SENTINEL)
            if not merged.is_complete(required):
                next_question = schema.question_for(
                    merged.missing_fields(required)[0]
                )
        # Story 12.14 - the LLM still didn't bind the customer's reply to the
        # field we just asked. For a numeric field, capture a plain count
        # deterministically ("1" → vehicle_count=1) so the funnel advances
        # instead of re-asking the same question forever. Declines are already
        # handled above; non-numeric fields keep relying on the LLM (Layer A).
        if (
            pending_field is not None
            and pending_field in schema.numeric_keys()
            and merged.get(pending_field) is None
            and not is_decline(question, normalizer=self._normalizer)
        ):
            count = _parse_count(question)
            if count is not None:
                merged = merged.with_field(pending_field, count)
                if not merged.is_complete(required):
                    next_question = schema.question_for(
                        merged.missing_fields(required)[0]
                    )
        # Not complete → keep scoping: forward the next-field question.
        if not merged.is_complete(required):
            # Story 16 (round-16 R16-2) — «<день1> или <день2> в HH:MM» → an
            # explicit per-day verdict.
            multi_date = await self._maybe_answer_multi_date(
                question=question, ctx=ctx, stage_before=STAGE_SCOPING
            )
            if multi_date is not None:
                return multi_date
            # Story 12.58 (round-14) — a mid-scoping availability INQUIRY gets a
            # plain verdict (no handoff/HITL), checked before the busy-intercept.
            inquiry = await self._maybe_answer_availability_inquiry(
                question=question, ctx=ctx, merged_intent=merged
            )
            if inquiry is not None:
                return inquiry
            # Story 12.60 (round-14 R14-1) — a vague time mid-scoping gets the
            # window checked + a concrete slot proposed, never a decline.
            vague = await self._maybe_answer_vague_window(
                question=question,
                ctx=ctx,
                merged_intent=merged,
                stage_before=STAGE_SCOPING,
            )
            if vague is not None:
                return vague
            # Story 12.25 — when this turn introduced a concrete date+time
            # (greeting opener with no time + first scoping reply, or a
            # customer changing their requested time mid-funnel), verify
            # the slot now instead of finishing scoping first.
            intercept = await self._maybe_intercept_busy_slot(
                ctx=ctx,
                existing_intent=existing,
                merged_intent=merged,
                stage_before=STAGE_SCOPING,
            )
            if intercept is not None:
                return intercept
            await self._persist(
                ctx=ctx, current_stage=STAGE_SCOPING, intent=merged
            )
            logger.info(
                "sales_answerer_handled",
                extra={
                    "trace_id": ctx.trace_id,
                    "stage_before": STAGE_SCOPING,
                    "stage_after": STAGE_SCOPING,
                    "fields_extracted": sorted(
                        name
                        for name, value in extracted.items()
                        if value is not None
                    ),
                },
            )
            return AnswerResult(
                handled=True,
                text=next_question,
                metadata={
                    "answerer": NAME,
                    "stage_before": STAGE_SCOPING,
                    "stage_after": STAGE_SCOPING,
                },
            )

        # Complete → the LLM `next_question` is meaningless (no missing field),
        # so we ignore it. Story 12.05 — the scoping → pitching transition is the
        # tour_preview media moment; the dispatch is awaited so a failure can
        # append a textual fallback to the completion line.
        media_metadata, dispatch_fallback = await self._fire_media_moment(
            ctx=ctx,
            intent=merged,
            purpose="tour_preview",
        )
        return await self._complete_booking(
            ctx=ctx,
            intent=merged,
            stage_before=STAGE_SCOPING,
            base_metadata=media_metadata,
            dispatch_fallback=dispatch_fallback,
            question=question,
        )

    async def _handle_awaiting_time(
        self,
        *,
        question: str,
        ctx: AnswerContext,
        state: dict[str, Any],
    ) -> AnswerResult:
        """Follow-up after we asked for the date+time (Story 12.10).

        Re-extracts the customer's reply (so a concrete date+time is merged in)
        and re-runs completion. We already asked once, so ``_complete_booking``
        does NOT ask again (``stage_before == STAGE_AWAITING_TIME``): a present
        time runs the slot check (confirm / offer alternative); still no time
        hands off to a human — never a second ask.
        """
        outcome = await self._extract_and_merge(
            question=question,
            ctx=ctx,
            state=state,
            stage_label=STAGE_AWAITING_TIME,
            schema=self._resolve_schema(ctx),
        )
        if isinstance(outcome, AnswerResult):
            return outcome
        merged, _next_question, _extracted = outcome
        # Story 12.62 (round-15) — when we asked only for the time (date already
        # known), a bare-time reply ("в 15:00") would REPLACE dates and drop the
        # date (intent_merge replaces). Re-attach the prior date so the slot
        # check gets a full date+time.
        merged = self._preserve_prior_date(merged=merged, state=state, ctx=ctx)
        return await self._complete_booking(
            ctx=ctx,
            intent=merged,
            stage_before=STAGE_AWAITING_TIME,
            question=question,
        )

    async def _handle_pitching(
        self,
        *,
        question: str,
        ctx: AnswerContext,
        state: dict[str, Any],
    ) -> AnswerResult:
        """Interpret a follow-up after an offer/handoff parked us in pitching.

        Priority (Story 12.19): (a) a parseable date in the reply is a
        counter-offer — re-run completion against the NEW time; (b) an
        acceptance of the slot we just offered confirms it (naming it) and
        closes; (c) closure or any other reply hands off to the operator
        WITHOUT re-checking the stale slot, so the bot never repeats the
        "time busy" line on every turn.
        """
        intent = Intent.from_dict(state.get("collected_intent") or {})
        last_proposal = state.get("last_proposal")
        now = self._clock()

        # (a) Counter-offer — a new parseable date overrides the stale one.
        merged = self._merge_dates_from_customer_message(
            existing_intent=intent, question=question, now=now
        )
        if merged.dates != intent.dates:
            return await self._complete_booking(
                ctx=ctx, intent=merged, stage_before=STAGE_PITCHING
            )

        # (a2) Time-only counter-offer (Story 12.51, round-11 R11-1): the
        # customer names a NEW clock time, the date implied by the slot we just
        # offered ("а давайте тогда в 12:00", "what about 10am?"). Re-check THAT
        # time before any acceptance check, so a leading "давайте" never books
        # the bot's own 08:00 instead of the customer's 12:00.
        counter = self._timeonly_counteroffer_start(
            question=question, last_proposal=last_proposal, now=now
        )
        if counter is not None:
            return await self._complete_booking(
                ctx=ctx,
                intent=replace(intent, dates=counter.strftime("%Y-%m-%d %H:%M")),
                stage_before=STAGE_PITCHING,
            )

        # (b) Acceptance of the slot we offered → confirm it (named) and close.
        if isinstance(last_proposal, dict) and is_acceptance(
            question, normalizer=self._normalizer
        ):
            iso = last_proposal.get("alternative_iso")
            slot_dt = datetime.fromisoformat(iso) if iso else None
            return await self._confirm_slot(ctx=ctx, intent=intent, slot_dt=slot_dt)

        # (c) Closure / anything else → hand off; never repeat the busy line.
        closure = is_no_more(question, normalizer=self._normalizer)
        return await self._handoff_after_pitching_followup(
            ctx=ctx, intent=intent, closure=closure
        )

    def _pitching_offered_slot(self, last_proposal: Any) -> datetime | None:
        """The alternative datetime we offered this pitching turn, or ``None``.

        Mirrors ``_confirm_slot``'s trust in the stored ISO (written by our own
        ``_persist``) — no defensive parse.
        """
        if isinstance(last_proposal, dict):
            iso = last_proposal.get("alternative_iso")
            if iso:
                return datetime.fromisoformat(iso)
        return None

    def _timeonly_counteroffer_start(
        self, *, question: str, last_proposal: Any, now: datetime
    ) -> datetime | None:
        """A pitching reply that names only a clock time → the new requested
        start (date carried from the offered slot), or ``None`` (Story 12.51).

        The bot's own offered time is excluded, so restating it ("давайте в
        08:00") is acceptance, not a counter; a single remaining time wins, so
        a correction naming both ("именно в 12:00, а не в 08:00") still picks
        12:00. More than one new time, or none, is ambiguous → ``None``.
        """
        tz = now.tzinfo
        offered = self._pitching_offered_slot(last_proposal)
        if tz is None or offered is None:
            return None
        proposal_hm = (offered.hour, offered.minute)
        candidates = [hm for hm in extract_all_clocks(question) if hm != proposal_hm]
        if len(candidates) != 1:
            return None
        hour, minute = candidates[0]
        return datetime(offered.year, offered.month, offered.day, hour, minute, tzinfo=tz)

    async def _confirm_slot(
        self,
        *,
        ctx: AnswerContext,
        intent: Intent,
        slot_dt: datetime | None,
    ) -> AnswerResult:
        """Confirm an accepted slot (naming it) + hand off; park in closing.

        ``slot_dt`` names the confirmed time; ``None`` (accepted but no concrete
        slot to name) falls back to the generic completion line. Autonomous
        booking is out of scope — the operator (already escalated) finalises.
        """
        if slot_dt is not None:
            if ctx.language == "en":
                day_month = f"{_MONTHS_EN[slot_dt.month]} {slot_dt.day}"
            else:
                day_month = f"{slot_dt.day} {_MONTHS_GENITIVE[slot_dt.month]}"
            text = localize(
                PITCHING_ACCEPT_CONFIRM_LINE,
                PITCHING_ACCEPT_CONFIRM_LINE_EN,
                language=ctx.language,
            ).format(day_month=day_month, time=slot_dt.strftime("%H:%M"))
            turn_kind = "pitching_accept_confirmed"
        else:
            text = localize(
                SCOPING_COMPLETE_HANDOFF_LINE,
                SCOPING_COMPLETE_HANDOFF_LINE_EN,
                language=ctx.language,
            )
            turn_kind = "pitching_accept_no_slot"
        await self._persist(
            ctx=ctx,
            current_stage=STAGE_CLOSING,
            intent=intent,
            last_proposal=None,
        )
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": STAGE_PITCHING,
                "stage_after": STAGE_CLOSING,
                "sales_turn_kind": turn_kind,
                "hitl_reason": HITL_REASON_SCOPING_COMPLETE,
            },
        )
        return AnswerResult(
            handled=True,
            text=text,
            response_mode=RESPONSE_MODE_SALES_ESCALATION,
            metadata={
                "answerer": NAME,
                "stage_before": STAGE_PITCHING,
                "stage_after": STAGE_CLOSING,
                "sales_turn_kind": turn_kind,
                "escalate": True,
                "hitl_reason": HITL_REASON_SCOPING_COMPLETE,
                "escalation_context": _format_intent_summary(intent),
            },
        )

    async def _handoff_after_pitching_followup(
        self,
        *,
        ctx: AnswerContext,
        intent: Intent,
        closure: bool,
    ) -> AnswerResult:
        """A pitching reply that is neither a counter-offer nor an acceptance.

        Speaks the generic completion line (NEVER the busy line) and parks in
        closing so a further reply stays handed off — the re-ask loop is dead.
        """
        await self._persist(
            ctx=ctx,
            current_stage=STAGE_CLOSING,
            intent=intent,
            last_proposal=None,
        )
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": STAGE_PITCHING,
                "stage_after": STAGE_CLOSING,
                "sales_turn_kind": "pitching_followup",
                "sales_closure_detected": closure,
                "hitl_reason": HITL_REASON_SCOPING_COMPLETE,
            },
        )
        return AnswerResult(
            handled=True,
            text=localize(
                SCOPING_COMPLETE_HANDOFF_LINE,
                SCOPING_COMPLETE_HANDOFF_LINE_EN,
                language=ctx.language,
            ),
            response_mode=RESPONSE_MODE_SALES_ESCALATION,
            metadata={
                "answerer": NAME,
                "stage_before": STAGE_PITCHING,
                "stage_after": STAGE_CLOSING,
                "sales_turn_kind": "pitching_followup",
                "sales_closure_detected": closure,
                "escalate": True,
                "hitl_reason": HITL_REASON_SCOPING_COMPLETE,
                "escalation_context": _format_intent_summary(intent),
            },
        )

    def _preserve_prior_date(
        self, *, merged: Intent, state: dict[str, Any], ctx: AnswerContext
    ) -> Intent:
        """Story 12.62 (round-15) — re-attach a previously-collected date when
        this turn's reply is time-only. ``intent_merge`` REPLACES ``dates``, so a
        bare "в 15:00" after we asked only-for-time would drop the date. When the
        merged dates carries a clock but no parseable full date, and the prior
        stored dates had a parseable date, rebuild "<YYYY-MM-DD> HH:MM"."""
        tz = ZoneInfo(ctx.timezone)
        new_dates = merged.dates if isinstance(merged.dates, str) else None
        if not new_dates:
            return merged
        if (
            extract_requested_start(text=new_dates, now=ctx.now, project_tz=tz)
            is not None
        ):
            return merged  # already a full date+time
        clocks = extract_all_clocks(new_dates)
        if not clocks:
            return merged  # no time either → nothing to combine
        prior = Intent.from_dict(state.get("collected_intent") or {})
        prior_dates = prior.dates if isinstance(prior.dates, str) else None
        prior_date = (
            extract_requested_date(text=prior_dates, now=ctx.now, project_tz=tz)
            if prior_dates
            else None
        )
        if prior_date is None:
            return merged
        hour, minute = clocks[0]
        return replace(
            merged, dates=f"{prior_date.isoformat()} {hour:02d}:{minute:02d}"
        )

    async def _complete_booking(
        self,
        *,
        ctx: AnswerContext,
        intent: Intent,
        stage_before: str,
        base_metadata: dict[str, Any] | None = None,
        dispatch_fallback: bool = False,
        question: str | None = None,
    ) -> AnswerResult:
        """Scoping is complete - check the requested time, then confirm/hand off.

        Decision table:
          * requested time is BUSY → offer the nearest free slot (→ proposing),
            or hand off if no slot / no proposer.
          * requested time is FREE → confirm it's free + hand off.
          * calendar disabled / no concrete time / not connected / error → hand
            off with the generic completion line (a human picks up).
        """
        base_metadata = base_metadata or {}
        # Story 12.61 (round-15) — a COMPLETE booking with a VAGUE time
        # ("во второй половине дня") proposes a concrete slot in that window,
        # same as the greeting/scoping hook — instead of falling to the generic
        # ask-for-time. The handler no-ops (returns None) when there's no vague
        # window or a concrete time IS present, so concrete bookings are
        # unaffected. ``question`` is None for legacy callers (no vague check).
        if question is not None:
            vague = await self._maybe_answer_vague_window(
                question=question,
                ctx=ctx,
                merged_intent=intent,
                stage_before=stage_before,
            )
            if vague is not None:
                return vague
        # Story 12.10 — no concrete time yet, but the calendar can check one:
        # ask for date+time instead of a blind hand off. Bounded to one ask —
        # the follow-up arrives with stage_before == STAGE_AWAITING_TIME.
        if stage_before != STAGE_AWAITING_TIME and await self._should_ask_for_time(
            ctx=ctx, intent=intent
        ):
            return await self._ask_for_time(
                ctx=ctx,
                intent=intent,
                stage_before=stage_before,
                base_metadata=base_metadata,
                dispatch_fallback=dispatch_fallback,
            )
        requested = await self._check_requested_slot(ctx=ctx, intent=intent)
        if requested is not None and requested.status == STATUS_UNAVAILABLE:
            return await self._propose_alternative_or_handoff(
                ctx=ctx,
                intent=intent,
                stage_before=stage_before,
                alternative=requested.alternative,
                base_metadata=base_metadata,
                dispatch_fallback=dispatch_fallback,
                reason=requested.reason,
            )
        # Story 12.32 (D1) — a concrete time WAS given but the calendar could not
        # verify it (not connected / reconnect needed / provider error). Never
        # collapse this into the accept-shaped handoff below: hand off flagged
        # UNVERIFIED so the operator confirms the exact time. (``requested is
        # None`` — no concrete time / calendar disabled — still uses the generic
        # handoff: there was nothing to verify.)
        if requested is not None and requested.status in (
            STATUS_NOT_CONNECTED,
            STATUS_ERROR,
        ):
            return await self._handoff_unverified_slot(
                ctx=ctx,
                intent=intent,
                stage_before=stage_before,
                status=requested.status,
                reason=requested.reason,
                base_metadata=base_metadata,
                dispatch_fallback=dispatch_fallback,
            )
        free = requested is not None and requested.status == STATUS_AVAILABLE
        return await self._handoff_after_scoping(
            ctx=ctx,
            intent=intent,
            stage_before=stage_before,
            free=free,
            base_metadata=base_metadata,
            dispatch_fallback=dispatch_fallback,
        )

    async def _ask_for_time(
        self,
        *,
        ctx: AnswerContext,
        intent: Intent,
        stage_before: str,
        base_metadata: dict[str, Any],
        dispatch_fallback: bool,
    ) -> AnswerResult:
        """Ask the customer for the desired date+time; park in awaiting_time.

        A plain handled reply (no escalation / HITL ticket). The stage
        transition is what bounds the ask to a single round.
        """
        # Story 12.62 (round-15) — if the customer already gave a date but no
        # time, ask only for the time (the date is preserved on the reply by
        # ``_preserve_prior_date``); otherwise ask for date+time.
        tz = ZoneInfo(ctx.timezone)
        has_date = bool(intent.dates) and (
            extract_requested_date(text=intent.dates, now=ctx.now, project_tz=tz)
            is not None
        )
        text = localize(
            ASK_FOR_TIME_ONLY_LINE if has_date else ASK_FOR_TIME_LINE,
            ASK_FOR_TIME_ONLY_LINE_EN if has_date else ASK_FOR_TIME_LINE_EN,
            language=ctx.language,
        )
        if dispatch_fallback:
            text = f"{text}\n{MATERIAL_DISPATCH_FALLBACK_LINE}"
        await self._persist(
            ctx=ctx, current_stage=STAGE_AWAITING_TIME, intent=intent
        )
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": stage_before,
                "stage_after": STAGE_AWAITING_TIME,
                "sales_turn_kind": "awaiting_time_prompt",
            },
        )
        return AnswerResult(
            handled=True,
            text=text,
            metadata={
                "answerer": NAME,
                "stage_before": stage_before,
                "stage_after": STAGE_AWAITING_TIME,
                "sales_turn_kind": "awaiting_time_prompt",
                **base_metadata,
            },
        )

    async def _calendar_booking_context(
        self, *, ctx: AnswerContext
    ) -> _CalBookingCtx | None:
        """Resolve the calendar-actionable context, or ``None``.

        ``None`` whenever the booking cannot be checked against the calendar:
        no calendar repo / project, calendar disabled, no settings row, or not
        exactly one active service to anchor the slot duration. Shared by
        ``_check_requested_slot`` and ``_should_ask_for_time`` so the gate never
        drifts between them.
        """
        if self._calendar_settings_repo is None or ctx.project_id is None:
            return None
        project_id = ctx.project_id
        enabled = await asyncio.to_thread(
            self._calendar_settings_repo.is_enabled, project_id
        )
        if not enabled:
            return None
        settings = await asyncio.to_thread(
            self._calendar_settings_repo.get, project_id
        )
        if settings is None:
            return None
        rules = await asyncio.to_thread(
            self._calendar_settings_repo.list_service_rules, project_id
        )
        active = [rule for rule in rules if getattr(rule, "name", None)]
        if len(active) != 1:
            return None
        return _CalBookingCtx(
            project_id=project_id,
            settings=settings,
            project_tz=ZoneInfo(settings.project_timezone),
            service_rule=active[0],
        )

    async def _should_ask_for_time(
        self, *, ctx: AnswerContext, intent: Intent
    ) -> bool:
        """True iff the booking is calendar-actionable but has no concrete time.

        Exactly the case where asking for a date+time is worthwhile: calendar
        enabled with one anchoring service, yet ``intent.dates`` carries no
        parseable date+time. When the calendar can't check (disabled / no single
        service / no project) we return False and the caller hands off as before.
        """
        cal = await self._calendar_booking_context(ctx=ctx)
        if cal is None:
            return False
        dates_text = intent.dates if isinstance(intent.dates, str) else None
        requested_start = (
            extract_requested_start(
                text=dates_text, now=ctx.now, project_tz=cal.project_tz
            )
            if dates_text
            else None
        )
        return requested_start is None

    async def _check_requested_slot(
        self, *, ctx: AnswerContext, intent: Intent
    ) -> Any | None:
        """Validate the customer's concrete requested time, or ``None``.

        Returns ``None`` (→ plain hand off) whenever the calendar cannot give a
        confident verdict: calendar wired-off / disabled / no single active
        service, or no parseable date+time in ``intent.dates``. Otherwise
        returns a ``RequestedAvailability``.
        """
        cal = await self._calendar_booking_context(ctx=ctx)
        if cal is None:
            return None
        dates_text = intent.dates if isinstance(intent.dates, str) else None
        if not dates_text:
            return None
        requested_start = extract_requested_start(
            text=dates_text, now=ctx.now, project_tz=cal.project_tz
        )
        if requested_start is None:
            return None
        operator = cal.settings.calendar_operator
        operator_chat_id = (
            self._operator_chat_resolver(operator)
            if operator and self._operator_chat_resolver is not None
            else None
        )
        return await check_requested_availability(
            project_id=cal.project_id,
            requested_start=requested_start,
            operator=operator,
            operator_chat_id=operator_chat_id,
            service_rule=cal.service_rule,
            token_provider=self._calendar_token_provider,
            freebusy_client=self._calendar_freebusy_client,
            now=ctx.now,
            project_tz=cal.project_tz,
            lookahead_days=cal.settings.lookahead_days,
            country_code=ctx.country_code,
            trace_id=ctx.trace_id,
        )

    async def _propose_alternative_or_handoff(
        self,
        *,
        ctx: AnswerContext,
        intent: Intent,
        stage_before: str,
        alternative: datetime | None,
        base_metadata: dict[str, Any],
        dispatch_fallback: bool,
        reason: str | None = None,
    ) -> AnswerResult:
        """Requested time is unavailable - offer an alternative or hand off.

        Story 12.22 — when we can name a nearest free slot we offer it and
        park in ``pitching`` so ``_handle_pitching`` can interpret the
        customer's reply (accept / counter-offer / closure) on the next turn;
        the HITL ticket is created only when the customer accepts (via
        ``_confirm_slot``) or abandons the offer (via
        ``_handoff_after_pitching_followup``). When no alternative is
        available the customer has nothing to accept onto — we keep the
        immediate handoff there so a human picks up.
        """
        offered = alternative is not None
        if offered:
            slot = _format_alternative_tail(alternative, ctx.language)
            turn_kind = "scoping_complete_busy_alternative"
        else:
            slot = " " + localize(
                SCOPING_COMPLETE_HANDOFF_LINE,
                SCOPING_COMPLETE_HANDOFF_LINE_EN,
                language=ctx.language,
            )
            turn_kind = "scoping_complete_busy_no_slot"
        # Story 12.29 — lead line reflects *why* the slot is unavailable; the
        # alternative-offer / handoff tail and stage transition are unchanged.
        # Story 12.47 - both the lead line and tail mirror the turn's language.
        lead_line = localize(
            _UNAVAILABLE_LEAD_LINES.get(reason, SLOT_BUSY_LINE),
            _UNAVAILABLE_LEAD_LINES_EN.get(reason, SLOT_BUSY_LINE_EN),
            language=ctx.language,
        )
        text = f"{lead_line}{slot}"
        if dispatch_fallback:
            text = f"{text}\n{MATERIAL_DISPATCH_FALLBACK_LINE}"
        # Story 12.19 — remember the offered slot so a confirmation next turn
        # (``_handle_pitching``) can recognise it; ``None`` when no slot was
        # offered (nothing to accept onto).
        await self._persist(
            ctx=ctx,
            current_stage=STAGE_PITCHING,
            intent=intent,
            last_proposal=(
                {"alternative_iso": alternative.isoformat()} if offered else None
            ),
        )
        log_extra = {
            "trace_id": ctx.trace_id,
            "stage_before": stage_before,
            "stage_after": STAGE_PITCHING,
            "sales_turn_kind": turn_kind,
        }
        if not offered:
            log_extra["hitl_reason"] = HITL_REASON_SCOPING_COMPLETE
        logger.info("sales_answerer_handled", extra=log_extra)
        metadata: dict[str, Any] = {
            "answerer": NAME,
            "stage_before": stage_before,
            "stage_after": STAGE_PITCHING,
            "sales_turn_kind": turn_kind,
            **base_metadata,
        }
        if offered:
            # Story 12.22 — defer escalation. The customer still has to accept
            # the offered slot; a HITL ticket fires on their next-turn reply.
            return AnswerResult(handled=True, text=text, metadata=metadata)
        metadata.update(
            {
                "escalate": True,
                "hitl_reason": HITL_REASON_SCOPING_COMPLETE,
                "escalation_context": _format_intent_summary(intent),
            }
        )
        return AnswerResult(
            handled=True,
            text=text,
            response_mode=RESPONSE_MODE_SALES_ESCALATION,
            metadata=metadata,
        )

    async def _maybe_answer_vague_window(
        self,
        *,
        question: str,
        ctx: AnswerContext,
        merged_intent: Intent,
        stage_before: str,
    ) -> AnswerResult | None:
        """Story 12.60 (round-14 R14-1) — an in-scope booking with a VAGUE time
        («…во второй половине дня…», no concrete clock) gets the day's window
        checked and a concrete free slot proposed, NEVER an out-of-scope decline.
        Parks in pitching with the proposed slot so the customer's «да» / a
        counter-time reply is handled by the existing accept/counter machinery
        (date carried from the offered slot). ``None`` when there's no vague
        window, no parseable date, a concrete time IS present, or the calendar
        can't propose a slot — those fall through to the normal flow.
        """
        window = detect_vague_window(question)
        if window is None:
            return None
        cal = await self._calendar_booking_context(ctx=ctx)
        if cal is None:
            return None
        tz = cal.project_tz
        dates_text = (
            merged_intent.dates if isinstance(merged_intent.dates, str) else None
        )
        # Vague case only: no concrete clock in the stored dates or the message.
        concrete = (
            extract_requested_start(text=dates_text, now=ctx.now, project_tz=tz)
            if dates_text
            else None
        )
        if concrete is None:
            concrete = extract_requested_start(
                text=question, now=ctx.now, project_tz=tz
            )
        if concrete is not None:
            return None
        target_date = (
            extract_requested_date(text=dates_text, now=ctx.now, project_tz=tz)
            if dates_text
            else None
        ) or extract_requested_date(text=question, now=ctx.now, project_tz=tz)
        if target_date is None:
            return None
        start_hour, end_hour = window
        operator = cal.settings.calendar_operator
        operator_chat_id = (
            self._operator_chat_resolver(operator)
            if operator and self._operator_chat_resolver is not None
            else None
        )
        # Scan the window hour-by-hour for the FIRST free slot inside it, so an
        # "afternoon" ask is offered an afternoon time (not the day's opening
        # hour). If the whole window is busy, fall back to the calendar's nearest
        # free slot (outside the window) so we still propose something, never
        # decline. (≤6 checks; only on a rare vague-time turn.)
        proposed: datetime | None = None
        fallback: datetime | None = None
        for hour in range(start_hour, end_hour):
            candidate = datetime(
                target_date.year, target_date.month, target_date.day, hour, 0,
                tzinfo=tz,
            )
            availability = await check_requested_availability(
                project_id=cal.project_id,
                requested_start=candidate,
                operator=operator,
                operator_chat_id=operator_chat_id,
                service_rule=cal.service_rule,
                token_provider=self._calendar_token_provider,
                freebusy_client=self._calendar_freebusy_client,
                now=ctx.now,
                project_tz=tz,
                lookahead_days=cal.settings.lookahead_days,
                country_code=ctx.country_code,
                trace_id=ctx.trace_id,
            )
            if availability.status == STATUS_AVAILABLE:
                proposed = candidate
                break
            if (
                availability.status == STATUS_UNAVAILABLE
                and availability.alternative is not None
            ):
                fallback = availability.alternative
        proposed = proposed or fallback
        if proposed is None:
            return None  # nothing free to propose — fall through
        text = localize(
            VAGUE_WINDOW_OFFER_LINE,
            VAGUE_WINDOW_OFFER_LINE_EN,
            language=ctx.language,
        ).format(time=proposed.strftime("%H:%M"))
        # Park in pitching with the proposed slot: the next turn («да» / a
        # concrete counter-time) is handled by _handle_pitching with the date
        # carried from this offer (Story 12.51).
        await self._persist(
            ctx=ctx,
            current_stage=STAGE_PITCHING,
            intent=merged_intent,
            last_proposal={"alternative_iso": proposed.isoformat()},
        )
        logger.info(
            "sales_vague_window_offered",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": stage_before,
                "proposed_iso": proposed.isoformat(),
            },
        )
        return AnswerResult(
            handled=True,
            text=text,
            metadata={
                "answerer": NAME,
                "stage_before": stage_before,
                "stage_after": STAGE_PITCHING,
                "sales_turn_kind": "vague_window_offer",
            },
        )

    async def _maybe_answer_multi_date(
        self, *, question: str, ctx: AnswerContext, stage_before: str
    ) -> AnswerResult | None:
        """Story 16 (round-16 R16-2) — «<день1> или <день2> в HH:MM» gets an
        EXPLICIT per-day verdict («6 июня в 12:00 - свободно; 7 июня в 12:00 -
        свободно. Какой день вам удобнее?»), so the customer can tell which day
        is confirmed instead of one unstated verdict. ``None`` unless the turn
        offers two distinct dates with a clock and the calendar can verify both.
        Russian-only by design (gated on «или»)."""
        if not re.search(r"\bили\b", question, re.IGNORECASE | re.UNICODE):
            return None
        cal = await self._calendar_booking_context(ctx=ctx)
        if cal is None:
            return None
        tz = cal.project_tz
        clocks = extract_all_clocks(question)
        if not clocks:
            return None  # no time → let the slot-fill flow ask for one
        hour, minute = clocks[0]
        dates: list[date] = []
        for part in re.split(r"\bили\b", question, flags=re.IGNORECASE | re.UNICODE):
            parsed = extract_requested_date(text=part, now=ctx.now, project_tz=tz)
            if parsed is not None and parsed not in dates:
                dates.append(parsed)
        if len(dates) < 2:
            return None  # not actually two distinct date options
        operator = cal.settings.calendar_operator
        operator_chat_id = (
            self._operator_chat_resolver(operator)
            if operator and self._operator_chat_resolver is not None
            else None
        )
        verdicts: list[str] = []
        for day in dates[:2]:
            start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            availability = await check_requested_availability(
                project_id=cal.project_id,
                requested_start=start,
                operator=operator,
                operator_chat_id=operator_chat_id,
                service_rule=cal.service_rule,
                token_provider=self._calendar_token_provider,
                freebusy_client=self._calendar_freebusy_client,
                now=ctx.now,
                project_tz=tz,
                lookahead_days=cal.settings.lookahead_days,
                country_code=ctx.country_code,
                trace_id=ctx.trace_id,
            )
            if availability.status == STATUS_AVAILABLE:
                word = "свободно"
            elif availability.status == STATUS_UNAVAILABLE:
                word = "занято"
            else:
                return None  # can't verify both → fall through to the normal flow
            verdicts.append(
                f"{day.day} {_MONTHS_GENITIVE[day.month]} "
                f"в {start.strftime('%H:%M')} - {word}"
            )
        logger.info(
            "sales_multi_date_verdict",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": stage_before,
                "dates": [d.isoformat() for d in dates[:2]],
            },
        )
        return AnswerResult(
            handled=True,
            text="; ".join(verdicts) + ". Какой день вам удобнее?",
            metadata={
                "answerer": NAME,
                "sales_turn_kind": "multi_date_verdict",
                "suppress_followup": True,
            },
        )

    async def _maybe_answer_availability_inquiry(
        self,
        *,
        question: str,
        ctx: AnswerContext,
        merged_intent: Intent,
    ) -> AnswerResult | None:
        """Story 12.58 (round-14) - a customer ASKING whether a concrete slot is
        free («…в 16:30 свободно?») gets a plain verdict (free / busy + nearest
        free / off-hours / past), with NO booking handoff and NO HITL ticket.
        ``None`` when the turn isn't an inquiry, carries no concrete time, or the
        calendar can't verify it - those fall through to the normal flow.
        """
        if not is_availability_inquiry(question):
            return None
        cal = await self._calendar_booking_context(ctx=ctx)
        if cal is None:
            return None
        dates_text = (
            merged_intent.dates if isinstance(merged_intent.dates, str) else None
        )
        start = (
            extract_requested_start(
                text=dates_text, now=ctx.now, project_tz=cal.project_tz
            )
            if dates_text
            else None
        )
        if start is None:
            start = extract_requested_start(
                text=question, now=ctx.now, project_tz=cal.project_tz
            )
        if start is None:
            return None  # no concrete time → not a concrete-slot inquiry
        operator = cal.settings.calendar_operator
        operator_chat_id = (
            self._operator_chat_resolver(operator)
            if operator and self._operator_chat_resolver is not None
            else None
        )
        availability = await check_requested_availability(
            project_id=cal.project_id,
            requested_start=start,
            operator=operator,
            operator_chat_id=operator_chat_id,
            service_rule=cal.service_rule,
            token_provider=self._calendar_token_provider,
            freebusy_client=self._calendar_freebusy_client,
            now=ctx.now,
            project_tz=cal.project_tz,
            lookahead_days=cal.settings.lookahead_days,
            country_code=ctx.country_code,
            trace_id=ctx.trace_id,
        )
        if availability.status == STATUS_AVAILABLE:
            text = localize(
                SLOT_FREE_INQUIRY_LINE,
                SLOT_FREE_INQUIRY_LINE_EN,
                language=ctx.language,
            )
        elif availability.status == STATUS_UNAVAILABLE:
            lead = localize(
                _UNAVAILABLE_LEAD_LINES.get(availability.reason, SLOT_BUSY_LINE),
                _UNAVAILABLE_LEAD_LINES_EN.get(
                    availability.reason, SLOT_BUSY_LINE_EN
                ),
                language=ctx.language,
            )
            tail = (
                _format_alternative_tail(availability.alternative, ctx.language)
                if availability.alternative is not None
                else ""
            )
            text = f"{lead}{tail}"
        else:  # not connected / error - can't verify; let the normal flow decide
            return None
        logger.info(
            "sales_availability_inquiry_answered",
            extra={
                "trace_id": ctx.trace_id,
                "requested_start_iso": start.isoformat(),
                "status": availability.status,
            },
        )
        # A pure question: verdict only — no escalation, no HITL, no funnel
        # mutation, and no "still thinking about booking?" nudge.
        return AnswerResult(
            handled=True,
            text=text,
            metadata={
                "answerer": NAME,
                "sales_turn_kind": "availability_inquiry",
                "suppress_followup": True,
            },
        )

    async def _maybe_intercept_busy_slot(
        self,
        *,
        ctx: AnswerContext,
        existing_intent: Intent,
        merged_intent: Intent,
        stage_before: str,
    ) -> AnswerResult | None:
        """Verify a newly-concrete requested time and offer alternatives early.

        Story 12.25 - closes the gap between Story 12.22's busy-check (fires
        at scoping completion in ``_complete_booking``) and the customer's
        first turn carrying a concrete date+time. Fires only when the
        parsed ``requested_start`` from ``merged_intent.dates`` is concrete
        AND different from the prior turn's parse - so we don't re-check
        the same time on every scoping turn. ``None`` whenever the calendar
        cannot give a confident verdict (disabled / multi-service / no
        single active rule / not connected / error) — the eventual
        ``_complete_booking`` re-runs the same check.
        """
        cal = await self._calendar_booking_context(ctx=ctx)
        if cal is None:
            return None
        existing_dates_text = (
            existing_intent.dates
            if isinstance(existing_intent.dates, str)
            else None
        )
        merged_dates_text = (
            merged_intent.dates if isinstance(merged_intent.dates, str) else None
        )
        existing_start = (
            extract_requested_start(
                text=existing_dates_text, now=ctx.now, project_tz=cal.project_tz
            )
            if existing_dates_text
            else None
        )
        new_start = (
            extract_requested_start(
                text=merged_dates_text, now=ctx.now, project_tz=cal.project_tz
            )
            if merged_dates_text
            else None
        )
        # Compare parsed datetimes, not strings: "14:00 завтра" and
        # "завтра в 14:00" parse the same but differ as strings.
        if new_start is None or new_start == existing_start:
            return None
        operator = cal.settings.calendar_operator
        operator_chat_id = (
            self._operator_chat_resolver(operator)
            if operator and self._operator_chat_resolver is not None
            else None
        )
        availability = await check_requested_availability(
            project_id=cal.project_id,
            requested_start=new_start,
            operator=operator,
            operator_chat_id=operator_chat_id,
            service_rule=cal.service_rule,
            token_provider=self._calendar_token_provider,
            freebusy_client=self._calendar_freebusy_client,
            now=ctx.now,
            project_tz=cal.project_tz,
            lookahead_days=cal.settings.lookahead_days,
            country_code=ctx.country_code,
            trace_id=ctx.trace_id,
        )
        # Mirror ``_complete_booking:1102`` — only ``STATUS_UNAVAILABLE``
        # triggers a busy response. ``STATUS_NOT_CONNECTED`` / ``STATUS_ERROR``
        # fall through silently so we never escalate from the early gate
        # on infra hiccups.
        if availability.status != STATUS_UNAVAILABLE:
            return None
        logger.info(
            "sales_early_busy_intercepted",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": stage_before,
                "requested_start_iso": new_start.isoformat(),
                "alternative_iso": (
                    availability.alternative.isoformat()
                    if availability.alternative is not None
                    else None
                ),
            },
        )
        return await self._propose_alternative_or_handoff(
            ctx=ctx,
            intent=merged_intent,
            stage_before=stage_before,
            alternative=availability.alternative,
            base_metadata={},
            dispatch_fallback=False,
            reason=availability.reason,
        )

    async def _maybe_intercept_price_ask(
        self, *, question: str, ctx: AnswerContext, merged: Intent
    ) -> AnswerResult | None:
        """Answer a price ask that lands on the first (greeting) turn.

        Story 12.28 — ``classify_turn`` already recognises a price ask, but it
        was only consulted mid-funnel (scoping / pitching) via
        ``_maybe_handle_aside``. On first contact ``state is None`` so the
        greeting handler ran instead, extracted the fields, was forbidden to
        quote a price, and asked for a date - silently dropping the customer's
        question. Routes to the existing ``_handle_pricing`` with a synthetic
        state carrying the just-extracted ``merged`` intent, so headcount/etc.
        inform the quote and are persisted (never re-asked). ``None`` when
        pricing is not configured or the turn is not a price ask — the greeting
        flow then continues unchanged.
        """
        if self._price_lookup is None:
            return None
        if classify_turn(question, normalizer=self._normalizer).kind != "price_ask":
            return None
        synthetic_state = {
            "current_stage": STAGE_NEW,
            "collected_intent": merged.to_dict(),
        }
        return await self._handle_pricing(
            question=question, ctx=ctx, state=synthetic_state
        )

    async def _handoff_after_scoping(
        self,
        *,
        ctx: AnswerContext,
        intent: Intent,
        stage_before: str,
        free: bool,
        base_metadata: dict[str, Any],
        dispatch_fallback: bool,
    ) -> AnswerResult:
        """Confirm the booking + open a HITL ticket carrying the collected intent."""
        text = (
            localize(SLOT_FREE_HANDOFF_LINE, SLOT_FREE_HANDOFF_LINE_EN, language=ctx.language)
            if free
            else localize(
                SCOPING_COMPLETE_HANDOFF_LINE,
                SCOPING_COMPLETE_HANDOFF_LINE_EN,
                language=ctx.language,
            )
        )
        if dispatch_fallback:
            text = f"{text}\n{MATERIAL_DISPATCH_FALLBACK_LINE}"
        # Story 12.19 - the free / can't-verify handoff offers no slot to accept.
        await self._persist(
            ctx=ctx,
            current_stage=STAGE_PITCHING,
            intent=intent,
            last_proposal=None,
        )
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": stage_before,
                "stage_after": STAGE_PITCHING,
                "sales_turn_kind": "scoping_complete",
                "slot_free": free,
                "hitl_reason": HITL_REASON_SCOPING_COMPLETE,
            },
        )
        return AnswerResult(
            handled=True,
            text=text,
            response_mode=RESPONSE_MODE_SALES_ESCALATION,
            metadata={
                "answerer": NAME,
                "stage_before": stage_before,
                "stage_after": STAGE_PITCHING,
                "sales_turn_kind": "scoping_complete",
                "escalate": True,
                "hitl_reason": HITL_REASON_SCOPING_COMPLETE,
                "escalation_context": _format_intent_summary(intent),
                **base_metadata,
            },
        )

    async def _handoff_unverified_slot(
        self,
        *,
        ctx: AnswerContext,
        intent: Intent,
        stage_before: str,
        status: str,
        reason: str | None,
        base_metadata: dict[str, Any],
        dispatch_fallback: bool,
    ) -> AnswerResult:
        """Story 12.32 (D1) - concrete time given, but the calendar could not be
        consulted (``not_connected`` / ``error:<reason>``). Hand off WITHOUT
        implying the slot is free: a distinct "I'll check it" line + a HITL
        ticket flagged ``calendar_verified=False`` (with the reason) and an
        operator-visible warning in the escalation context, so a human confirms
        the exact time. Mirrors the date-proposer's ``provider_error``
        escalation; the ``escalation_context`` prefix rides into the operator DM
        (``api/main.py``).
        """
        text = localize(
            SLOT_UNVERIFIED_HANDOFF_LINE,
            SLOT_UNVERIFIED_HANDOFF_LINE_EN,
            language=ctx.language,
        )
        if dispatch_fallback:
            text = f"{text}\n{MATERIAL_DISPATCH_FALLBACK_LINE}"
        unverified_reason = status if reason is None else f"{status}:{reason}"
        await self._persist(
            ctx=ctx,
            current_stage=STAGE_PITCHING,
            intent=intent,
            last_proposal=None,
        )
        logger.info(
            "sales_answerer_handled",
            extra={
                "trace_id": ctx.trace_id,
                "stage_before": stage_before,
                "stage_after": STAGE_PITCHING,
                "sales_turn_kind": "scoping_complete_unverified",
                "calendar_verified": False,
                "calendar_unverified_reason": unverified_reason,
                "hitl_reason": HITL_REASON_CALENDAR_UNVERIFIED,
            },
        )
        return AnswerResult(
            handled=True,
            text=text,
            response_mode=RESPONSE_MODE_SALES_ESCALATION,
            metadata={
                "answerer": NAME,
                "stage_before": stage_before,
                "stage_after": STAGE_PITCHING,
                "sales_turn_kind": "scoping_complete_unverified",
                "escalate": True,
                "hitl_reason": HITL_REASON_CALENDAR_UNVERIFIED,
                "calendar_verified": False,
                "calendar_unverified_reason": unverified_reason,
                "escalation_context": (
                    f"⚠️ Календарь не проверен ({unverified_reason}); "
                    f"подтвердите точное время. {_format_intent_summary(intent)}"
                ),
                **base_metadata,
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
                text=localize(
                    EMPTY_CATALOG_ESCALATION_LINE,
                    EMPTY_CATALOG_ESCALATION_LINE_EN,
                    language=ctx.language,
                ),
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
            # Story 12.44 (round-8 N2) — pass the configured service catalog so
            # the lookup won't quote a price for a service the customer didn't
            # ask about (a багги question must not return the квадроцикл rate).
            services = await asyncio.to_thread(
                self._services_repo.list_for_project,
                project_id=int(ctx.project_id or 0),
            )
            service_names = [
                row.name.strip()
                for row in services
                if getattr(row, "name", None) and row.name.strip()
            ]
            outcome = await self._price_lookup.lookup(
                project_id=ctx.project_id,
                intent=existing_intent,
                question=question,
                service_names=service_names,
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
                question=question,
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
        question: str = "",
    ) -> AnswerResult:
        persona = self._persona_getter()
        system = _PRICING_HIT_PROMPT_TEMPLATE.format(
            persona=persona, snippet=outcome.snippet
        ) + reply_language_directive(question)  # N3 - mirror the customer's language
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
        """LLM quote disagreed with the snippet - never deliver a wrong price.

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

        now = self._clock()
        merged_intent = self._merge_dates_from_customer_message(
            existing_intent=existing_intent,
            question=question,
            now=now,
        )

        # Counter-offer beats acceptance (Story 12.21, mirroring 12.19's
        # ``_handle_pitching`` ordering). A reply carrying a NEW/different
        # parseable date ("давайте на 1 июня") is a counter-offer for that
        # date — not a confirmation of the slot we already proposed — even
        # though its acceptance lemma ("давайте") would match ``is_acceptance``.
        # Treat the reply as acceptance only when it carries no new date
        # (``merged_intent.dates == existing_intent.dates``) and a prior
        # proposal exists — otherwise the first proposing turn is the date hint,
        # not a confirmation. A new date falls through to ``propose`` below.
        if (
            merged_intent.dates == existing_intent.dates
            and last_proposal is not None
            and is_acceptance(question, normalizer=self._normalizer)
        ):
            return await self._transition_to_closing(
                ctx=ctx, intent=existing_intent
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

    def _dates_with_raw_fallback(
        self, *, intent: Intent, question: str, ctx: AnswerContext
    ) -> Intent:
        """Adopt the raw customer message as ``dates`` when the LLM's stored
        ``dates`` can't be parsed into a concrete start but the raw text can.

        Round-8 N1: the scoping/greeting LLM sometimes stores a numeric date
        WITHOUT its co-located time ("03.06"); ``extract_requested_start`` then
        returns ``None``, the deterministic busy check is skipped, and a busy
        numeric slot is handed off unchecked. The raw message always carries
        date+time. Conservative — only overrides when the stored value does NOT
        already parse, so a clean LLM date (relative / written month / already
        date+time) is left intact. The tz only affects the parse-or-not decision
        here; the downstream check re-parses with the real project tz.
        """
        tz = ZoneInfo(ctx.timezone)
        stored = intent.dates if isinstance(intent.dates, str) else None
        if (
            stored
            and extract_requested_start(text=stored, now=ctx.now, project_tz=tz)
            is not None
        ):
            return intent
        if (
            extract_requested_start(text=question, now=ctx.now, project_tz=tz)
            is not None
        ):
            return replace(intent, dates=question.strip())
        return intent

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
        stands. Story 12.48 (round-10 N1) — ``parse_russian_date_span`` only
        handles relative/written-month dates, so a numeric counter-offer
        ("03.06 в 16:00") in pitching went undetected and skipped the busy
        check; also accept a date the deterministic ``extract_requested_start``
        can parse (which covers numeric/ISO/slash forms).
        """
        if parse_russian_date_span(
            question, now=now.date()
        ) is None and not self._question_carries_concrete_start(question, now):
            return existing_intent
        return replace(existing_intent, dates=question.strip())

    def _question_carries_concrete_start(
        self, question: str, now: datetime
    ) -> bool:
        """True when the raw question parses to a concrete date+time (round-10
        N1) - covers numeric forms ``parse_russian_date_span`` misses."""
        tz = now.tzinfo
        return (
            tz is not None
            and extract_requested_start(text=question, now=now, project_tz=tz)
            is not None
        )

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
            # The customer is in proposing but hasn't pinned a date yet -
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
        """Customer accepted the proposal - speak the handoff line + escalate.

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
            text=localize(
                CLOSING_HANDOFF_LINE, CLOSING_HANDOFF_LINE_EN, language=ctx.language
            ),
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
        """Closing-stage follow-ups stay in closing - the handoff is sticky."""
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
            text=localize(
                CLOSING_HANDOFF_LINE, CLOSING_HANDOFF_LINE_EN, language=ctx.language
            ),
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
    "HITL_REASON_SCOPING_COMPLETE",
    "LlmSchemaViolation",
    "MATERIAL_DISPATCH_FALLBACK_LINE",
    "MaterialDispatcher",
    "NAME",
    "PITCHING_ACCEPT_CONFIRM_LINE",
    "PRICING_MISS_FALLBACK",
    "PROPOSAL_AMBIGUOUS_SERVICE_CLARIFIER",
    "PROPOSAL_FALLBACK_CALENDAR_DISABLED",
    "PROPOSAL_FALLBACK_UNAVAILABLE",
    "RESPONSE_MODE_SALES_ESCALATION",
    "SCOPING_COMPLETE_HANDOFF_LINE",
    "SLOT_BUSY_LINE",
    "SLOT_FREE_HANDOFF_LINE",
    "STAGE_AWAITING_OPERATOR_PRICE",
    "STAGE_CLOSING",
    "STAGE_PITCHING",
    "STAGE_PRICING",
    "STAGE_PROPOSING",
    "STAGE_SCOPING",
    "SalesPersonaAnswerer",
]

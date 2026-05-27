import asyncio
import hmac
import logging
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
from cryptography.fernet import Fernet
from fastapi import Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from platform_common.app_factory import create_service_app
from platform_common.settings import get_settings
from services.api.app.admin_auth import (
    AdminAuthRepository,
    AdminAuthService,
    AdminSession,
    InvalidLoginCode,
    SessionPrincipal,
    wire_admin_auth_routes,
)
from services.api.app.admin_files import wire_admin_files_routes
from services.api.app.admin_nl_ops import (
    OP_FILE_ATTACH,
    OP_OPERATOR_ATTACH,
    OP_OPERATOR_DETACH,
    OP_PROJECT_CREATE,
    OP_PROJECT_RENAME,
    AdminNlOpSession,
    AdminNlOpsRepository,
    InvalidConfirmToken,
    SessionNotPending,
)
from services.api.app.admin_rag_inspect import wire_admin_rag_inspect_routes
from services.api.app.answer_trace import AnswerTraceRepository
from services.api.app.answerers import AnswerContext, AnswerPipeline
from services.api.app.answerers.grounded_rag import GroundedRagAnswerer
from services.api.app.answerers.weather_client import WeatherClient
from services.api.app.backups import BackupError, BackupRepository
from services.api.app.calendar.access_token_cache import AccessTokenProvider
from services.api.app.calendar.authorization import (
    authorize_calendar_config,
    authorize_calendar_disconnect,
    authorize_service_remove,
)
from services.api.app.calendar.availability_answerer import (
    RESPONSE_MODE_ESCALATION,
    CalendarAvailabilityAnswerer,
)
from services.api.app.calendar.calendar_client import CalendarFreeBusyClient
from services.api.app.calendar.calendar_service_alias_hint_repository import (
    init_calendar_service_alias_hint_schema,
)
from services.api.app.calendar.clarify_state_repository import (
    CalendarClarifyStateRepository,
)
from services.api.app.calendar.oauth import (
    CalendarOAuthClient,
    OAuthExchangeError,
)
from services.api.app.calendar.oauth_state_repository import (
    CalendarOAuthStateRepository,
    InvalidOAuthState,
)
from services.api.app.calendar.project_service_validation import (
    ProjectServiceValidationError,
    validate_project_service,
)
from services.api.app.calendar.project_services_repository import (
    ProjectService,
    ProjectServiceNotFound,
    ProjectServiceRepository,
    acquire_service_upsert_lock,
)
from services.api.app.calendar.service_rule_validation import (
    ServiceRuleValidationError,
    validate_service_rule,
)
from services.api.app.calendar.services_nl_op_session_repository import (
    init_services_nl_ops_schema,
)
from services.api.app.calendar.settings_repository import (
    CalendarSettingsRepository,
    ServiceRule,
)
from services.api.app.calendar.token_repository import (
    CalendarTokenRepository,
    TokenNotFound,
    init_token_schema,
)
from services.api.app.catalog_digest import (
    CatalogDigestRepository,
    CatalogDigestService,
)
from services.api.app.hitl import HitlTicketRepository
from services.api.app.incidents import IncidentRepository
from services.api.app.knowledge import KnowledgeCandidateRepository
from services.api.app.knowledge_moderation import KnowledgeModerationRepository
from services.api.app.nl_knowledge_ops import (
    NlKnowledgeOpsError,
    NlKnowledgeOpsRepository,
)
from services.api.app.openrouter_client import OpenRouterClient
from services.api.app.operator_files_admin import OperatorFilesAdminWriter
from services.api.app.operator_files_view import OperatorFilesView
from services.api.app.operators import (
    Operator,
    OperatorRepository,
    OperatorUsernameConflict,
)
from services.api.app.project_prompts import (
    PROMPT_NAME_LIST,
    PROMPT_NAMES,
    ProjectPromptRepository,
    PromptCurrent,
    PromptValueInvalid,
    PromptValueTooLarge,
    PromptVersion,
    PromptVersionNotFound,
    default_prompt,
)
from services.api.app.projects import (
    Project,
    ProjectReferenced,
    ProjectRepository,
    ProjectSlugConflict,
)
from services.api.app.rag import RagRepository
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.bootstrap import (
    init_schema as sales_bootstrap_init_schema,
)
from services.api.app.sales.client_materials_analyzer import (
    AnalysisOutcome,
    ClientMaterialsAnalyzer,
)
from services.api.app.sales.client_materials_repository import (
    ClientMaterial,
    ClientMaterialsRepository,
)
from services.api.app.sales.client_materials_selector import (
    ClientMaterialsSelector,
)
from services.api.app.sales.followup_cancel_hook import maybe_cancel
from services.api.app.sales.followup_fire_handler import FollowupFireHandler
from services.api.app.sales.followup_queue_repository import (
    REASON_PAST_INTENT_DATE,
    FollowupQueueRepository,
    FollowupRow,
)
from services.api.app.sales.price_lookup import PriceLookup
from services.api.app.sales.sales_persona_answerer import (
    RESPONSE_MODE_SALES_ESCALATION,
    SalesPersonaAnswerer,
)
from services.api.app.sales.services_extractor import (
    ExtractionOutcome,
    ServicesExtractor,
)
from services.api.app.sales.services_repository import (
    ServiceAlreadyExists,
    ServiceNotFound,
    ServicesRepository,
)
from services.api.app.sales.state_repository import (
    StateRepository as SalesStateRepository,
)
from services.api.app.services_nl_ops import (
    OP_SERVICE_REMOVE,
    ServicesNlOpsRepository,
    ServicesNlSession,
    apply_confirmed,
)
from services.api.app.services_nl_ops import (
    InvalidConfirmToken as ServicesNlInvalidConfirmToken,
)
from services.api.app.services_nl_ops import (
    NlOpSessionExpired as ServicesNlSessionExpired,
)
from services.api.app.services_nl_ops import (
    NlOpSessionNotFound as ServicesNlSessionNotFound,
)
from services.api.app.services_nl_ops import (
    NlOpSessionNotOwner as ServicesNlSessionNotOwner,
)
from services.api.app.services_nl_ops import (
    NlOpSessionNotPending as ServicesNlSessionNotPending,
)
from services.api.app.telegram_bot_sender import (
    TelegramBotSender,
    TelegramMediaSendError,
)
from services.api.app.telegram_notifier import TelegramIncidentNotifier
from services.api.app.trace_corrections import (
    BRANCH_MODERATION,
    BRANCH_PUBLISH,
    TraceCorrectionError,
    TraceCorrectionRepository,
)
from services.api.app.web_auth import WebAuthRepository

app = create_service_app("api")
logger = logging.getLogger(__name__)
openrouter_client = OpenRouterClient()
settings = get_settings()
incident_repository = IncidentRepository(
    db_path=settings.incident_db_path,
    dedup_window_seconds=settings.incident_dedup_window_seconds,
)
telegram_notifier = TelegramIncidentNotifier(
    bot_token=settings.telegram_bot_token,
    alert_chat_id=settings.telegram_alert_chat_id,
    alert_username=settings.telegram_alert_username,
    base_url=settings.telegram_bot_api_base_url,
)
hitl_ticket_repository = HitlTicketRepository(settings.hitl_ticket_db_path)
knowledge_candidate_repository = KnowledgeCandidateRepository(
    db_path=settings.knowledge_db_path,
    transcript_db_path=settings.persistence_db_path,
)
rag_repository = RagRepository(settings.rag_db_path)
knowledge_moderation_repository = KnowledgeModerationRepository(settings.knowledge_db_path)
telegram_bot_sender = TelegramBotSender(
    bot_token=settings.telegram_bot_token,
    base_url=settings.telegram_bot_api_base_url,
)
backup_repository = BackupRepository(
    db_path=settings.backup_db_path,
    archive_dir=settings.backup_archive_dir,
    source_paths=settings.backup_source_path_list(),
)
answer_trace_repository = AnswerTraceRepository(
    db_path=settings.answer_trace_db_path,
    snippet_max_chars=settings.answer_trace_snippet_max_chars,
)
nl_knowledge_ops_repository = NlKnowledgeOpsRepository(db_path=settings.nl_ops_db_path)
trace_correction_repository = TraceCorrectionRepository(db_path=settings.nl_ops_db_path)
weather_client = WeatherClient(base_url=settings.weather_provider_base_url)
project_repository = ProjectRepository(settings.projects_db_path)
operator_repository = OperatorRepository(settings.operators_db_path)
project_prompt_repository = ProjectPromptRepository(settings.hitl_ticket_db_path)
catalog_digest_service = CatalogDigestService(
    repository=CatalogDigestRepository(settings.rag_db_path),
    openrouter_client=openrouter_client,
    project_prompt_repository=project_prompt_repository,
)
admin_auth_repository = AdminAuthRepository(settings.admin_session_db_path)
admin_nl_ops_repository = AdminNlOpsRepository(settings.nl_ops_db_path)
# Epic 11: bootstrap the calendar DB idempotently. Calendar stays opt-in and
# disabled for every project until a settings row is explicitly enabled. The
# token table is created without an encryption key; crypto ops require one.
calendar_settings_repository = CalendarSettingsRepository(
    db_path=settings.calendar_db_path
)
calendar_oauth_state_repository = CalendarOAuthStateRepository(
    db_path=settings.calendar_db_path
)
# Epic 13 (story 13.02): canonical project_services repository, shared by the
# new /api/projects/{id}/services endpoints. The old /calendar alias endpoints
# still route through ``calendar_settings_repository``'s deprecated wrappers
# (which themselves delegate into the same underlying table) so Epic-11 callers
# continue to see identical behavior + just gain a deprecation log line.
project_services_repository = ProjectServiceRepository(
    db_path=settings.calendar_db_path
)
init_token_schema(settings.calendar_db_path)
# Epic 13 (story 13.01): bootstrap the operator-scoped services NL ops session
# table next to the existing admin_nl_op_sessions table. Both are idempotent.
init_services_nl_ops_schema(settings.nl_ops_db_path)
# Epic 13 (story 13.04): operator-scoped NL ops state machine + endpoints
# share the same DB file via the bootstrap above.
services_nl_ops_repository = ServicesNlOpsRepository(
    db_path=settings.nl_ops_db_path,
    pending_ttl_seconds=settings.services_nl_op_session_ttl_seconds,
)
# Epic 13 (story 13.03): bootstrap the per-(project, operator) dedup table for
# the ``/calendar_service`` migration-hint DM. Idempotent; lives in
# semantaix_nl_ops.db alongside services_nl_op_sessions.
init_calendar_service_alias_hint_schema(settings.nl_ops_db_path)


def _build_calendar_token_repository(app_settings) -> CalendarTokenRepository | None:
    """Encrypted refresh-token store; ``None`` until an encryption key is set.

    The token table is bootstrapped above regardless, so reads of an empty
    store stay cheap; the connect endpoints return 503 while the key is unset.
    """
    if not app_settings.calendar_token_encryption_key:
        return None
    return CalendarTokenRepository(
        db_path=app_settings.calendar_db_path,
        fernet=Fernet(app_settings.calendar_token_encryption_key),
    )


def _build_calendar_oauth_client(app_settings) -> CalendarOAuthClient | None:
    """Google OAuth client; ``None`` until client_id/secret/redirect_uri are set."""
    if not (
        app_settings.google_oauth_client_id
        and app_settings.google_oauth_client_secret
        and app_settings.google_oauth_redirect_uri
    ):
        return None
    return CalendarOAuthClient(
        client_id=app_settings.google_oauth_client_id,
        client_secret=app_settings.google_oauth_client_secret,
        redirect_uri=app_settings.google_oauth_redirect_uri,
    )


calendar_token_repository = _build_calendar_token_repository(settings)
calendar_oauth_client = _build_calendar_oauth_client(settings)
calendar_clarify_state_repository = CalendarClarifyStateRepository(
    db_path=settings.calendar_db_path
)


class _SystemClock:
    """Tz-aware UTC clock for the calendar token/freebusy collaborators."""

    def now(self) -> datetime:
        return datetime.now(UTC)


calendar_system_clock = _SystemClock()
calendar_http_client = httpx.AsyncClient(timeout=settings.calendar_http_timeout_seconds)


def _build_calendar_token_provider() -> AccessTokenProvider | None:
    """Single-flight access-token cache; ``None`` until OAuth + key are set."""
    if calendar_oauth_client is None or calendar_token_repository is None:
        return None
    return AccessTokenProvider(
        oauth_client=calendar_oauth_client,
        token_repo=calendar_token_repository,
        clock=calendar_system_clock,
        lock_factory=asyncio.Lock,
        incident_sink=incident_repository,
        notifier=telegram_bot_sender,
    )


def _build_calendar_freebusy_client() -> CalendarFreeBusyClient | None:
    """freeBusy httpx client; ``None`` until OAuth is configured."""
    if calendar_oauth_client is None:
        return None
    return CalendarFreeBusyClient(
        http_client=calendar_http_client,
        clock=calendar_system_clock,
        incident_sink=incident_repository,
        timeout_seconds=settings.calendar_http_timeout_seconds,
    )


calendar_token_provider = _build_calendar_token_provider()
calendar_freebusy_client = _build_calendar_freebusy_client()


def _resolve_calendar_operator_chat_id(operator: str) -> int | None:
    """Map the calendar operator's username to its Telegram chat_id (or None)."""
    record = operator_repository.find_by_username(operator)
    if record is None:
        return None
    return record.chat_id


web_auth_repository = WebAuthRepository(db_path=settings.web_auth_db_path)
admin_auth_service = AdminAuthService(
    web_auth_repository=web_auth_repository,
    hitl_repository=hitl_ticket_repository,
    telegram_bot_sender=telegram_bot_sender,
    settings=settings,
)
operator_files_view = OperatorFilesView(
    operator_files_db_path=settings.operator_files_db_path,
    knowledge_db_path=settings.knowledge_db_path,
)
operator_files_admin_writer = OperatorFilesAdminWriter(
    operator_files_db_path=settings.operator_files_db_path,
    knowledge_db_path=settings.knowledge_db_path,
    rag_db_path=settings.rag_db_path,
)
wire_admin_auth_routes(app, service=admin_auth_service)
wire_admin_files_routes(
    app,
    auth_service=admin_auth_service,
    files_view=operator_files_view,
    files_admin_writer=operator_files_admin_writer,
)
wire_admin_rag_inspect_routes(
    app,
    auth_service=admin_auth_service,
    rag_repository=rag_repository,
    operator_files_db_path=lambda: settings.operator_files_db_path,
    resolve_inbound_project_id=lambda chat_id: _resolve_inbound_project_id(chat_id),
    default_project_id=lambda: _default_project_id(),
    grounding_threshold=lambda: _effective_grounding_threshold(),
)


def _bootstrap_default_entities() -> None:
    """Idempotently ensure a `default` project and a primary operator row exist.

    Runs at module import so a fresh `docker compose up` always lands with the
    schema rows the rest of Epic 10 depends on. Tests can re-run it after
    rebinding repository db paths.
    """
    default_project = project_repository.ensure_default_project()
    primary_chat_id_raw = settings.hitl_primary_operator_chat_id
    primary_chat_id = None
    if primary_chat_id_raw is not None:
        try:
            primary_chat_id = int(primary_chat_id_raw)
        except (TypeError, ValueError):
            primary_chat_id = None
    operator_repository.ensure_default_operator(
        username=settings.hitl_primary_operator_username,
        project_id=default_project.id,
        chat_id=primary_chat_id,
    )


_bootstrap_default_entities()


def _effective_bot_persona() -> tuple[str, str]:
    return hitl_ticket_repository.get_bot_persona(
        default_first_name=settings.bot_persona_first_name,
        default_last_name=settings.bot_persona_last_name,
    )


def _effective_sales_persona_name() -> str:
    """Resolve the persona name passed to the sales LLM prompts.

    Joins the configurable first/last name (already used by the HITL bot
    identity) so the sales persona is the same human face the customer
    sees elsewhere.
    """
    first, last = _effective_bot_persona()
    return f"{first} {last}".strip() if last else first


# Epic 12 story 12.01: bootstrap every sales DB table + index in a single
# call so the schema is fully shaped before any repository handle opens
# the file. The repositories below still run their own per-table
# init_schema (idempotent) for tests that construct them in isolation.
sales_bootstrap_init_schema(settings.sales_db_path)

# Epic 12 story 12.03: construct the SalesPersonaAnswerer eagerly so the
# `sales_conversation_state` table is bootstrapped at startup. Story 12.09
# wires the answerer into `answer_pipeline` below, immediately BEFORE the
# calendar availability answerer. Construction is unconditional; the
# always-on activation gate inside the answerer (state row OR sales-intent
# regex match) handles dormancy without consulting the services catalog.
sales_state_repository = SalesStateRepository(db_path=settings.sales_db_path)
sales_services_repository = ServicesRepository(db_path=settings.sales_db_path)
sales_followup_repository = FollowupQueueRepository(
    db_path=settings.sales_db_path
)
client_materials_repository = ClientMaterialsRepository(
    db_path=settings.sales_db_path
)
client_materials_selector = ClientMaterialsSelector(
    repo=client_materials_repository
)


async def _in_process_material_dispatcher(
    *,
    chat_id: int,
    material_id: int,
    trace_id: str | None,
    caption_override: str | None = None,
) -> dict[str, object]:
    """Story 12.05 — call ``POST /sales/dispatch/material`` in-process.

    The sales answerer threads the trace_id through so both the textual
    pitch and the dispatch log land on the same ``answer_traces`` row.
    Internally calls :func:`sales_dispatch_material` directly so we don't
    pay the HTTP round-trip when both sides live in the api service.
    """
    request = SalesMaterialDispatchRequest(
        chat_id=chat_id,
        material_id=material_id,
        caption_override=caption_override,
        trace_id=trace_id,
    )
    return await sales_dispatch_material(
        request=request,
        _="internal",
    )


sales_price_lookup = PriceLookup(
    rag_retriever=rag_repository,
    normalizer=get_russian_normalizer(),
)
sales_persona_answerer = SalesPersonaAnswerer(
    state_repo=sales_state_repository,
    services_repo=sales_services_repository,
    openrouter=openrouter_client,
    normalizer=get_russian_normalizer(),
    clock=lambda: datetime.now(UTC),
    bot_persona_getter=_effective_sales_persona_name,
    rag_retriever=rag_repository,
    grounding_threshold_getter=lambda: _effective_grounding_threshold(),
    followup_repo=sales_followup_repository,
    price_lookup=sales_price_lookup,
    material_selector=client_materials_selector,
    material_dispatcher=_in_process_material_dispatcher,
)
client_materials_analyzer = ClientMaterialsAnalyzer(
    openrouter=openrouter_client,
    operator_files_view=operator_files_view,
    materials_repo=client_materials_repository,
)


class _ServicesExtractorRepoAdapter:
    """Adapts the canonical ``project_services_repository`` (Epic-13) to the
    ``ServicesExtractor``'s 12.01-shaped ``find_by_name`` + ``add`` surface.

    The extractor uses ``find_by_name`` solely to soft-skip an existing
    service — it never overwrites an operator's manually-crafted description
    on file upload (per the 12.05c story rules). ``add`` for a brand-new
    name maps to ``upsert`` on the underlying repo; since the SELECT-then-
    INSERT path of ``upsert`` is only reached when the row does not exist,
    the call cannot accidentally update a pre-existing row.
    """

    def __init__(self, *, repo: ProjectServiceRepository) -> None:
        self._repo = repo

    def find_by_name(
        self, *, project_id: int, name: str
    ) -> ProjectService | None:
        return self._repo.get_by_name(project_id=project_id, name=name)

    def add(
        self,
        *,
        project_id: int,
        name: str,
        description_md: str | None,
        tags: list[str],
        now: datetime,
    ) -> int:
        # ``now`` is accepted for the 12.01-shaped protocol but the canonical
        # repo stamps its own ``updated_at`` UTC string inside ``upsert``.
        created = self._repo.upsert(
            project_id=project_id,
            name=name,
            description=description_md,
            tags=tags,
        )
        return created.id


services_extractor = ServicesExtractor(
    openrouter=openrouter_client,
    operator_files_view=operator_files_view,
    services_repo=_ServicesExtractorRepoAdapter(
        repo=project_services_repository
    ),
)

sales_followup_fire_handler = FollowupFireHandler(
    followup_repo=sales_followup_repository,
    state_repo=sales_state_repository,
    openrouter=openrouter_client,
    telegram_sender=telegram_bot_sender,
    persona_getter=_effective_sales_persona_name,
    clock=lambda: datetime.now(UTC),
)


answer_pipeline = AnswerPipeline(
    [
        # Story 12.09: SalesPersonaAnswerer runs BEFORE CalendarAvailabilityAnswerer
        # so the sales funnel (greeting → scoping → pricing → proposing → closing)
        # owns the turn before scheduling questions ever reach the calendar. The
        # answerer's own activation gate (existing state OR sales-intent regex)
        # keeps the dormant cost at one cheap repo read + one regex match for
        # every non-sales inbound message.
        sales_persona_answerer,
        # Calendar availability runs next so an enabled project answers its own
        # scheduling questions before they ever reach RAG. A disabled project
        # (the default) is a cheap no-op skip, so this is a pure prepend with no
        # behaviour change when calendar is off.
        CalendarAvailabilityAnswerer(
            settings_repo=calendar_settings_repository,
            token_provider=calendar_token_provider,
            freebusy_client=calendar_freebusy_client,
            normalizer=get_russian_normalizer(),
            clarify_store=calendar_clarify_state_repository,
            operator_chat_resolver=_resolve_calendar_operator_chat_id,
        ),
        GroundedRagAnswerer(
            rag_repository=rag_repository,
            openrouter_client=openrouter_client,
            persona_reader=_effective_bot_persona,
            project_prompt_repository=project_prompt_repository,
            catalog_digest_service=catalog_digest_service,
            weather_client=weather_client,
            project_services_reader=project_services_repository,
        ),
    ]
)


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "api", "message": "Semantaix API"}


class AdminLoginRequestModel(BaseModel):
    admin_username: str


class AdminLoginVerifyRequest(BaseModel):
    admin_username: str
    code: str


def _ensure_admin_username(admin_username: str) -> None:
    if admin_username != settings.admin_telegram_username:
        raise HTTPException(status_code=403, detail="not_admin")


def require_admin_session(
    x_admin_session: Annotated[str | None, Header()] = None,
) -> AdminSession:
    if not x_admin_session:
        raise HTTPException(status_code=401, detail="missing_admin_session")
    session = admin_auth_repository.validate_session(x_admin_session)
    if session is None:
        raise HTTPException(status_code=401, detail="invalid_admin_session")
    return session


def require_admin_or_internal(
    x_admin_session: Annotated[str | None, Header()] = None,
    x_internal_token: Annotated[str | None, Header()] = None,
) -> str:
    """Accept either an admin session token or the configured internal token.

    Returns the principal identifier (admin username, or "internal" for
    bot-to-api server-to-server calls). Raises 401 if neither credential
    is presented.
    """
    expected = settings.admin_internal_token
    if x_internal_token and expected and hmac.compare_digest(
        x_internal_token, expected
    ):
        return "internal"
    if x_admin_session:
        session = admin_auth_repository.validate_session(x_admin_session)
        if session is not None:
            return session.admin_username
    raise HTTPException(status_code=401, detail="admin_auth_required")


def require_internal_token(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Service-to-service auth via ``Authorization: Bearer <internal_service_token>``.

    Mirrors the admin-files internal path but without ``as_user`` — the
    calendar connect/disconnect calls come from bot_gateway, not a web role.
    """
    expected = settings.internal_service_token
    if (
        expected
        and authorization
        and authorization.startswith("Bearer ")
        and hmac.compare_digest(authorization.removeprefix("Bearer "), expected)
    ):
        return "internal"
    raise HTTPException(status_code=401, detail="internal_auth_required")


def _project_to_dict(project: Project) -> dict[str, object]:
    return {
        "id": project.id,
        "slug": project.slug,
        "name": project.name,
        "description": project.description,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def _operator_to_dict(operator: Operator) -> dict[str, object]:
    return {
        "id": operator.id,
        "username": operator.username,
        "chat_id": operator.chat_id,
        "project_id": operator.project_id,
        "display_name": operator.display_name,
        "is_active": operator.is_active,
        "created_at": operator.created_at,
        "updated_at": operator.updated_at,
    }


@app.post("/admin/login/request")
async def admin_login_request(request: AdminLoginRequestModel) -> dict[str, object]:
    _ensure_admin_username(request.admin_username)
    admin_operator = operator_repository.find_by_username(request.admin_username)
    if admin_operator is None or admin_operator.chat_id is None:
        raise HTTPException(status_code=400, detail="admin_operator_chat_id_missing")
    code = admin_auth_repository.request_code(
        admin_username=request.admin_username,
        ttl_seconds=settings.admin_login_code_ttl_seconds,
    )
    minutes = max(1, settings.admin_login_code_ttl_seconds // 60)
    message = f"Ваш код входа: {code} (действителен {minutes} мин)"
    try:
        await telegram_bot_sender.send_message(
            chat_id=admin_operator.chat_id, text=message
        )
    except Exception as exc:  # broad: any DM failure surfaces as 502
        logger.warning("admin_login_code_dm_failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="telegram_dm_failed"
        ) from exc
    return {"requested": True}


@app.post("/admin/login/verify")
def admin_login_verify(request: AdminLoginVerifyRequest) -> dict[str, object]:
    _ensure_admin_username(request.admin_username)
    try:
        session = admin_auth_repository.consume_code(
            admin_username=request.admin_username,
            code=request.code,
            ttl_seconds=settings.admin_session_ttl_seconds,
        )
    except InvalidLoginCode as exc:
        raise HTTPException(status_code=401, detail="invalid_login_code") from exc
    return {
        "session_token": session.token,
        "expires_at": session.expires_at,
        "admin_username": session.admin_username,
    }


@app.post("/admin/logout")
def admin_logout(
    session: Annotated[AdminSession, Depends(require_admin_session)],
) -> dict[str, bool]:
    admin_auth_repository.revoke_session(session.token)
    return {"ok": True}


@app.get("/admin/session/check")
def admin_session_check(
    session: Annotated[AdminSession, Depends(require_admin_session)],
) -> dict[str, str]:
    return {
        "admin_username": session.admin_username,
        "expires_at": session.expires_at,
    }


class ProjectCreateRequest(BaseModel):
    slug: str
    name: str
    description: str | None = None


class ProjectUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class OperatorCreateRequest(BaseModel):
    username: str
    project_id: int
    chat_id: int | None = None
    display_name: str | None = None


class OperatorUpdateRequest(BaseModel):
    project_id: int | None = None
    chat_id: int | None = None
    display_name: str | None = None
    is_active: bool | None = None


class FileReassignRequest(BaseModel):
    project_id: int


@app.get("/projects")
def list_projects(
    _principal: Annotated[str, Depends(require_admin_or_internal)],
) -> dict[str, object]:
    items = [_project_to_dict(p) for p in project_repository.list_all()]
    return {"items": items}


@app.post("/projects")
def create_project(
    request: ProjectCreateRequest,
    _principal: Annotated[str, Depends(require_admin_or_internal)],
) -> dict[str, object]:
    try:
        project = project_repository.create(
            slug=request.slug,
            name=request.name,
            description=request.description,
        )
    except ProjectSlugConflict as exc:
        raise HTTPException(status_code=409, detail="project_slug_conflict") from exc
    return _project_to_dict(project)


@app.get("/projects/{slug}")
def get_project(
    slug: str,
    _principal: Annotated[str, Depends(require_admin_or_internal)],
) -> dict[str, object]:
    project = project_repository.get_by_slug(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="project_not_found")
    operators = operator_repository.list_by_project_id(project.id)
    payload = _project_to_dict(project)
    payload["operator_count"] = len(operators)
    payload["operators"] = [_operator_to_dict(op) for op in operators]
    return payload


@app.patch("/projects/{slug}")
def patch_project(
    slug: str,
    request: ProjectUpdateRequest,
    _principal: Annotated[str, Depends(require_admin_or_internal)],
) -> dict[str, object]:
    try:
        project = project_repository.update(
            slug=slug, name=request.name, description=request.description
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="project_not_found") from exc
    return _project_to_dict(project)


@app.delete("/projects/{slug}")
def delete_project(
    slug: str,
    _principal: Annotated[str, Depends(require_admin_or_internal)],
) -> dict[str, bool]:
    try:
        project_repository.delete(
            slug, is_referenced=operator_repository.any_referencing_project
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="project_not_found") from exc
    except ProjectReferenced as exc:
        raise HTTPException(status_code=409, detail="project_referenced") from exc
    return {"ok": True}


class PromptValueRequest(BaseModel):
    value: str


class PromptRestoreRequest(BaseModel):
    version: int


def _prompt_current_to_dict(current: PromptCurrent) -> dict[str, object]:
    return {
        "project_id": current.project_id,
        "prompt_name": current.prompt_name,
        "value": current.value,
        "version": current.version,
        "updated_at": current.updated_at,
        "updated_by": current.updated_by,
        "is_default": False,
    }


def _prompt_default_to_dict(project_id: int, name: str) -> dict[str, object]:
    return {
        "project_id": project_id,
        "prompt_name": name,
        "value": default_prompt(name),
        "version": 0,
        "updated_at": None,
        "updated_by": None,
        "is_default": True,
    }


def _prompt_version_to_dict(pv: PromptVersion) -> dict[str, object]:
    return {
        "version": pv.version,
        "value": pv.value,
        "edited_by": pv.edited_by,
        "created_at": pv.created_at,
    }


def _project_or_404(slug: str) -> Project:
    project = project_repository.get_by_slug(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="project_not_found")
    return project


def _ensure_known_prompt_name(name: str) -> None:
    if name not in PROMPT_NAMES:
        raise HTTPException(status_code=404, detail="unknown_prompt_name")


def _require_project_access(
    request: Request, slug: str, as_user: str | None
) -> tuple[Project, SessionPrincipal]:
    """Resolve principal and enforce admin-or-operator-of-this-project.

    Supports three credentials so both the admin web UI (X-Admin-Session)
    and the operator web UI (semantaix_session cookie) and bot-to-api
    calls (Authorization: Bearer + as_user) can reach these endpoints.
    """
    admin_session_token = request.headers.get("x-admin-session")
    if admin_session_token:
        admin_session = admin_auth_repository.validate_session(
            admin_session_token
        )
        if admin_session is None:
            raise HTTPException(status_code=401, detail="invalid_admin_session")
        project = _project_or_404(slug)
        return project, SessionPrincipal(
            username=admin_session.admin_username, role="admin"
        )
    principal = admin_auth_service.require_session_or_internal(request, as_user)
    project = _project_or_404(slug)
    if principal.role == "admin":
        return project, principal
    operator = operator_repository.find_by_username(principal.username)
    if operator is None or operator.project_id != project.id:
        raise HTTPException(status_code=403, detail="not_in_project")
    return project, principal


@app.get("/projects/{slug}/prompts")
def list_project_prompts(
    slug: str,
    request: Request,
    as_user: str | None = None,
) -> dict[str, object]:
    project, _ = _require_project_access(request, slug, as_user)
    by_name = {
        pc.prompt_name: pc
        for pc in project_prompt_repository.list_current(project.id)
    }
    items = [
        _prompt_current_to_dict(by_name[name])
        if name in by_name
        else _prompt_default_to_dict(project.id, name)
        for name in PROMPT_NAME_LIST
    ]
    return {
        "project_id": project.id,
        "project_slug": project.slug,
        "items": items,
    }


@app.get("/projects/{slug}/prompts/{name}")
def get_project_prompt(
    slug: str,
    name: str,
    request: Request,
    as_user: str | None = None,
) -> dict[str, object]:
    _ensure_known_prompt_name(name)
    project, _ = _require_project_access(request, slug, as_user)
    current = project_prompt_repository.get_current(
        project_id=project.id, prompt_name=name
    )
    if current is None:
        body = _prompt_default_to_dict(project.id, name)
    else:
        body = _prompt_current_to_dict(current)
    body["history"] = [
        _prompt_version_to_dict(pv)
        for pv in project_prompt_repository.list_versions(
            project_id=project.id, prompt_name=name
        )
    ]
    return body


@app.put("/projects/{slug}/prompts/{name}")
def put_project_prompt(
    slug: str,
    name: str,
    payload: PromptValueRequest,
    request: Request,
    as_user: str | None = None,
) -> dict[str, object]:
    _ensure_known_prompt_name(name)
    project, principal = _require_project_access(request, slug, as_user)
    try:
        version = project_prompt_repository.set(
            project_id=project.id,
            prompt_name=name,
            value=payload.value,
            edited_by=principal.username,
        )
    except PromptValueTooLarge as exc:
        raise HTTPException(
            status_code=413, detail="prompt_value_too_large"
        ) from exc
    except PromptValueInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "project_id": project.id,
        "prompt_name": name,
        "version": version,
    }


@app.post("/projects/{slug}/prompts/{name}/restore")
def restore_project_prompt(
    slug: str,
    name: str,
    payload: PromptRestoreRequest,
    request: Request,
    as_user: str | None = None,
) -> dict[str, object]:
    _ensure_known_prompt_name(name)
    project, principal = _require_project_access(request, slug, as_user)
    try:
        version = project_prompt_repository.restore(
            project_id=project.id,
            prompt_name=name,
            version=payload.version,
            edited_by=principal.username,
        )
    except PromptVersionNotFound as exc:
        raise HTTPException(status_code=404, detail="version_not_found") from exc
    return {
        "project_id": project.id,
        "prompt_name": name,
        "version": version,
    }


@app.post("/projects/{slug}/prompts/{name}/pending")
def arm_prompt_pending_edit(
    slug: str,
    name: str,
    request: Request,
    as_user: str | None = None,
) -> dict[str, object]:
    _ensure_known_prompt_name(name)
    project, principal = _require_project_access(request, slug, as_user)
    project_prompt_repository.arm_pending(
        user_username=principal.username,
        project_id=project.id,
        prompt_name=name,
    )
    return {
        "ok": True,
        "project_id": project.id,
        "project_slug": project.slug,
        "prompt_name": name,
        "armed_for": principal.username,
    }


@app.get("/pending-prompt-edits")
def peek_pending_prompt_edit(
    request: Request, as_user: str | None = None
) -> dict[str, object]:
    principal = admin_auth_service.require_session_or_internal(request, as_user)
    pending = project_prompt_repository.peek_pending(principal.username)
    if pending is None:
        raise HTTPException(status_code=404, detail="no_pending_edit")
    project = project_repository.get(pending.project_id)
    return {
        "project_id": pending.project_id,
        "project_slug": project.slug if project is not None else None,
        "prompt_name": pending.prompt_name,
        "expires_at": pending.expires_at,
    }


@app.delete("/pending-prompt-edits")
def cancel_pending_prompt_edit(
    request: Request, as_user: str | None = None
) -> dict[str, bool]:
    principal = admin_auth_service.require_session_or_internal(request, as_user)
    deleted = project_prompt_repository.cancel_pending(
        user_username=principal.username
    )
    return {"deleted": deleted}


@app.post("/pending-prompt-edits/consume")
def consume_pending_prompt_edit(
    payload: PromptValueRequest,
    request: Request,
    as_user: str | None = None,
) -> dict[str, object]:
    principal = admin_auth_service.require_session_or_internal(request, as_user)
    pending = project_prompt_repository.consume_pending(principal.username)
    if pending is None:
        raise HTTPException(status_code=404, detail="no_pending_edit")
    try:
        version = project_prompt_repository.set(
            project_id=pending.project_id,
            prompt_name=pending.prompt_name,
            value=payload.value,
            edited_by=principal.username,
        )
    except PromptValueTooLarge as exc:
        raise HTTPException(
            status_code=413, detail="prompt_value_too_large"
        ) from exc
    except PromptValueInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    project = project_repository.get(pending.project_id)
    return {
        "project_id": pending.project_id,
        "project_slug": project.slug if project is not None else None,
        "prompt_name": pending.prompt_name,
        "version": version,
    }


@app.get("/projects/{slug}/prompts/{name}/versions")
def list_project_prompt_versions(
    slug: str,
    name: str,
    request: Request,
    as_user: str | None = None,
    limit: int = 50,
) -> dict[str, object]:
    _ensure_known_prompt_name(name)
    project, _ = _require_project_access(request, slug, as_user)
    versions = project_prompt_repository.list_versions(
        project_id=project.id, prompt_name=name, limit=limit
    )
    return {
        "project_id": project.id,
        "prompt_name": name,
        "items": [_prompt_version_to_dict(pv) for pv in versions],
    }


@app.get("/operators")
def list_operators(
    _principal: Annotated[str, Depends(require_admin_or_internal)],
) -> dict[str, object]:
    items = [_operator_to_dict(op) for op in operator_repository.list_all()]
    return {"items": items}


@app.post("/operators")
def create_operator(
    request: OperatorCreateRequest,
    _principal: Annotated[str, Depends(require_admin_or_internal)],
) -> dict[str, object]:
    if project_repository.get(request.project_id) is None:
        raise HTTPException(status_code=400, detail="project_not_found")
    try:
        operator = operator_repository.create(
            username=request.username,
            project_id=request.project_id,
            chat_id=request.chat_id,
            display_name=request.display_name,
        )
    except OperatorUsernameConflict as exc:
        raise HTTPException(
            status_code=409, detail="operator_username_conflict"
        ) from exc
    return _operator_to_dict(operator)


@app.get("/operators/by-username/{username:path}")
def get_operator_by_username(username: str) -> dict[str, object]:
    """Internal endpoint used by bot_gateway; intentionally unauthenticated.

    Returns 404 when the operator does not exist. The bot_gateway treats
    404 as "non-operator sender" and 5xx as "fall back to primary".
    """
    operator = operator_repository.find_by_username(username)
    if operator is None:
        raise HTTPException(status_code=404, detail="operator_not_found")
    return _operator_to_dict(operator)


@app.patch("/operators/{username:path}")
def patch_operator(
    username: str,
    request: OperatorUpdateRequest,
    _principal: Annotated[str, Depends(require_admin_or_internal)],
) -> dict[str, object]:
    if (
        request.project_id is not None
        and project_repository.get(request.project_id) is None
    ):
        raise HTTPException(status_code=400, detail="project_not_found")
    try:
        operator = operator_repository.update(
            username=username,
            project_id=request.project_id,
            chat_id=request.chat_id,
            display_name=request.display_name,
            is_active=request.is_active,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="operator_not_found") from exc
    return _operator_to_dict(operator)


@app.get("/knowledge/candidates/by-operator-file/{short_id}")
def get_candidate_by_operator_short_id(short_id: str) -> dict[str, object]:
    """Internal endpoint used by bot_gateway admin commands.

    Finds the knowledge candidate that was uploaded via the operator file
    with the given short_id. Returns 404 when the upload predates the
    operator_short_id plumbing or did not originate from an operator
    upload.
    """
    candidate = knowledge_moderation_repository.find_by_operator_short_id(
        short_id
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="candidate_not_found")
    return {
        "candidate_id": candidate.id,
        "operator_short_id": candidate.operator_short_id,
        "project_id": candidate.project_id,
        "source_file_name": candidate.source_file_name,
        "uploaded_by_operator_username": candidate.uploaded_by_operator_username,
    }


@app.post("/knowledge/candidates/{candidate_id}/reassign")
def reassign_candidate(
    candidate_id: int,
    request: FileReassignRequest,
    _principal: Annotated[str, Depends(require_admin_or_internal)],
) -> dict[str, object]:
    if project_repository.get(request.project_id) is None:
        raise HTTPException(status_code=400, detail="project_not_found")
    try:
        knowledge_moderation_repository.get(candidate_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="candidate_not_found") from exc
    knowledge_moderation_repository.set_project_id(
        candidate_id=candidate_id, project_id=request.project_id
    )
    rag_repository.update_project_id_for_source(
        source_id=f"knowledge_candidate:{candidate_id}",
        project_id=request.project_id,
    )
    return {"candidate_id": candidate_id, "project_id": request.project_id}


class AdminNlOpProposeRequest(BaseModel):
    admin_username: str
    utterance: str


class AdminNlOpConfirmRequest(BaseModel):
    confirm_token: str


def _session_to_dict(session: AdminNlOpSession) -> dict[str, object]:
    return {
        "id": session.id,
        "admin_username": session.admin_username,
        "utterance": session.utterance,
        "op_type": session.op_type,
        "payload": session.payload,
        "status": session.status,
        "confirm_token": session.confirm_token,
        "preview": session.preview,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _apply_admin_nl_op(session: AdminNlOpSession) -> None:
    """Execute the side-effect for a confirmed admin NL op."""
    payload = session.payload
    if session.op_type == OP_PROJECT_CREATE:
        try:
            project_repository.create(
                slug=str(payload["slug"]),
                name=str(payload["name"]),
            )
        except ProjectSlugConflict as exc:
            raise HTTPException(
                status_code=409, detail="project_slug_conflict"
            ) from exc
        return
    if session.op_type == OP_PROJECT_RENAME:
        try:
            project_repository.update(
                slug=str(payload["slug"]), name=str(payload["name"])
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=404, detail="project_not_found"
            ) from exc
        return
    if session.op_type == OP_OPERATOR_ATTACH:
        project = project_repository.get_by_slug(str(payload["project_slug"]))
        if project is None:
            raise HTTPException(
                status_code=400, detail="project_not_found"
            )
        chat_id = payload.get("chat_id")
        try:
            operator_repository.create(
                username=str(payload["username"]),
                project_id=project.id,
                chat_id=int(chat_id) if chat_id is not None else None,
            )
        except OperatorUsernameConflict as exc:
            raise HTTPException(
                status_code=409, detail="operator_username_conflict"
            ) from exc
        return
    if session.op_type == OP_OPERATOR_DETACH:
        try:
            operator_repository.update(
                username=str(payload["username"]), is_active=False
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=404, detail="operator_not_found"
            ) from exc
        return
    if session.op_type == OP_FILE_ATTACH:
        project = project_repository.get_by_slug(str(payload["project_slug"]))
        if project is None:
            raise HTTPException(
                status_code=400, detail="project_not_found"
            )
        candidate = knowledge_moderation_repository.find_by_operator_short_id(
            str(payload["short_id"])
        )
        if candidate is None:
            raise HTTPException(
                status_code=404, detail="candidate_not_found"
            )
        knowledge_moderation_repository.set_project_id(
            candidate_id=candidate.id, project_id=project.id
        )
        rag_repository.update_project_id_for_source(
            source_id=f"knowledge_candidate:{candidate.id}",
            project_id=project.id,
        )
        return
    # OP_CLARIFY or unknown — nothing to apply (caller validates state).
    raise HTTPException(status_code=400, detail="unconfirmable_op_type")


@app.post("/admin/nl-ops")
def admin_nl_ops_propose(
    request: AdminNlOpProposeRequest,
    _principal: Annotated[str, Depends(require_admin_or_internal)],
) -> dict[str, object]:
    _ensure_admin_username(request.admin_username)
    session = admin_nl_ops_repository.propose(
        admin_username=request.admin_username, utterance=request.utterance
    )
    return _session_to_dict(session)


@app.post("/admin/nl-ops/{session_id}/confirm")
def admin_nl_ops_confirm(
    session_id: int,
    request: AdminNlOpConfirmRequest,
    _principal: Annotated[str, Depends(require_admin_or_internal)],
) -> dict[str, object]:
    try:
        admin_nl_ops_repository.get(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="session_not_found") from exc
    try:
        confirmed = admin_nl_ops_repository.confirm(
            session_id=session_id, confirm_token=request.confirm_token
        )
    except InvalidConfirmToken as exc:
        raise HTTPException(status_code=401, detail="invalid_confirm_token") from exc
    except SessionNotPending as exc:
        raise HTTPException(status_code=409, detail=f"session_status:{exc}") from exc
    _apply_admin_nl_op(confirmed)
    return _session_to_dict(confirmed)


@app.post("/admin/nl-ops/{session_id}/cancel")
def admin_nl_ops_cancel(
    session_id: int,
    _principal: Annotated[str, Depends(require_admin_or_internal)],
) -> dict[str, object]:
    try:
        cancelled = admin_nl_ops_repository.cancel(session_id=session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="session_not_found") from exc
    except SessionNotPending as exc:
        raise HTTPException(status_code=409, detail=f"session_status:{exc}") from exc
    return _session_to_dict(cancelled)


@app.get("/admin/nl-ops/latest-pending")
def admin_nl_ops_latest_pending(
    admin_username: str,
    _principal: Annotated[str, Depends(require_admin_or_internal)],
) -> dict[str, object]:
    """Returns the most recent pending session for an admin, or {found: false}."""
    session = admin_nl_ops_repository.latest_pending_for(admin_username)
    if session is None:
        return {"found": False}
    payload = _session_to_dict(session)
    payload["found"] = True
    return payload


class InboundMessageRequest(BaseModel):
    text: str
    chat_id: int | None = None
    trace_id: str | None = None
    customer_username: str | None = None


class IncidentEventRequest(BaseModel):
    fingerprint: str
    severity: str
    summary: str


class HitlRouteRequest(BaseModel):
    operator_username: str | None = None


class HitlReplyRequest(BaseModel):
    operator_username: str
    reply_text: str


class KnowledgeExtractRequest(BaseModel):
    conversation_id: int | None = None


class RagIngestRequest(BaseModel):
    source_id: str
    text: str


class RagRetrieveRequest(BaseModel):
    query: str
    limit: int = 3
    project_id: int | None = None


class KnowledgeCandidateCreateRequest(BaseModel):
    text: str


class KnowledgeCandidateApproveRequest(BaseModel):
    edited_text: str | None = None


class BackupRestoreRequest(BaseModel):
    confirm_token: str
    target_root: str


class NlOpProposeRequest(BaseModel):
    user_id: str
    utterance: str
    tenant_id: str | None = None


class NlOpConfirmRequest(BaseModel):
    confirm_token: str


class TraceOpenRequest(BaseModel):
    tenant_id: str
    user_id: str


class TraceCorrectionRequest(BaseModel):
    tenant_id: str
    user_id: str
    edited_text: str
    branch: str


class OperatorUploadRequest(BaseModel):
    operator_username: str
    source_file_type: str
    source_file_name: str | None = None
    stored_binary_path: str | None = None
    is_confidential: bool = False
    inline_text: str | None = None
    operator_short_id: str | None = None
    project_id: int | None = None
    project_slug: str | None = None


class BotPersonaRequest(BaseModel):
    first_name: str
    last_name: str = ""
    description: str | None = None
    short_description: str | None = None
    updated_by: str


_PERSONA_NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё \-']{0,31}$")
_PERSONA_DESCRIPTION_MAX = 512
_PERSONA_SHORT_DESCRIPTION_MAX = 120


def _validate_persona_name(value: str) -> str:
    candidate = value.strip()
    if not _PERSONA_NAME_RE.fullmatch(candidate):
        raise HTTPException(status_code=422, detail="invalid_persona_name")
    return candidate


def _effective_hitl_operator_username() -> str:
    return (
        hitl_ticket_repository.get_runtime_config("hitl_primary_operator_username")
        or settings.hitl_primary_operator_username
    )


def _effective_hitl_operator_chat_id() -> str | None:
    return (
        hitl_ticket_repository.get_runtime_config("hitl_primary_operator_chat_id")
        or settings.hitl_primary_operator_chat_id
    )


def _effective_inbound_ack_message(project_id: int | None = None) -> str:
    """Resolve the inbound ack with per-project precedence.

    Order: project-scoped override → global runtime_config → settings default.
    """
    if project_id is not None:
        override = project_prompt_repository.get(
            project_id=project_id, prompt_name="inbound_ack"
        )
        if override:
            return override
    return (
        hitl_ticket_repository.get_runtime_config("inbound_ack_message")
        or settings.inbound_ack_message
    )


def _effective_default_country() -> str:
    return (
        hitl_ticket_repository.get_runtime_config("default_country_code")
        or settings.default_country_code
    )


def _effective_default_timezone() -> str:
    return (
        hitl_ticket_repository.get_runtime_config("default_timezone")
        or settings.default_timezone
    )


def _effective_default_location() -> str:
    return (
        hitl_ticket_repository.get_runtime_config("default_location")
        or settings.default_location
    )


def _effective_default_language() -> str:
    return (
        hitl_ticket_repository.get_runtime_config("default_language")
        or settings.default_language
    )


def _effective_grounding_threshold() -> float:
    raw = hitl_ticket_repository.get_runtime_config("rag_grounding_score_threshold")
    if raw is None:
        return settings.rag_grounding_score_threshold
    try:
        return float(raw)
    except ValueError:
        return settings.rag_grounding_score_threshold


def _pick_assignee_for_chat(chat_id: int | None) -> str:
    """Prefer the operator who already handles this chat; fall back to primary.

    Sticky routing: if the customer's most recent ticket has an assigned
    `operator_username` that maps to an active operator in the registry,
    re-assign the new ticket to them. Otherwise fall back to the
    primary operator configured in settings/runtime config. Pure
    backwards-compat for single-operator deployments.
    """
    primary = _effective_hitl_operator_username()
    if chat_id is None:
        return primary
    try:
        latest = hitl_ticket_repository.latest_for_chat(chat_id)
    except AttributeError:
        latest = None
    if latest is None or not latest.operator_username:
        return primary
    if latest.operator_username == primary:
        return primary
    operator = operator_repository.find_by_username(latest.operator_username)
    if operator is not None and operator.is_active:
        return operator.username
    return primary


def _resolve_inbound_project_id(chat_id: int | None) -> int | None:
    """Resolve the project_id for an incoming customer message.

    Looks at the most recent HITL ticket assigned to the chat; if it
    has an `operator_username` that maps to a registered operator with
    a project binding, that project scopes RAG retrieval. Falls back
    to the default project (id from `ensure_default_project`) so
    pre-Epic-10 deployments behave identically.
    """
    default_project_id = _default_project_id()
    if chat_id is None:
        logger.info(
            "inbound_project_resolved",
            extra={
                "chat_id": None,
                "resolution_path": "no_chat_id",
                "resolved_project_id": default_project_id,
                "default_project_id": default_project_id,
                "latest_ticket_id": None,
                "ticket_operator_username": None,
                "operator_project_id": None,
            },
        )
        return default_project_id
    try:
        ticket = hitl_ticket_repository.latest_for_chat(chat_id)
    except AttributeError:
        ticket = None
    ticket_id = ticket.id if ticket is not None else None
    ticket_op = ticket.operator_username if ticket is not None else None
    if ticket is not None and ticket.operator_username:
        operator = operator_repository.find_by_username(
            ticket.operator_username
        )
        if operator is not None:
            logger.info(
                "inbound_project_resolved",
                extra={
                    "chat_id": chat_id,
                    "resolution_path": "from_ticket_operator",
                    "resolved_project_id": operator.project_id,
                    "default_project_id": default_project_id,
                    "latest_ticket_id": ticket_id,
                    "ticket_operator_username": ticket_op,
                    "operator_project_id": operator.project_id,
                },
            )
            return operator.project_id
    logger.info(
        "inbound_project_resolved",
        extra={
            "chat_id": chat_id,
            "resolution_path": "default_fallback",
            "resolved_project_id": default_project_id,
            "default_project_id": default_project_id,
            "latest_ticket_id": ticket_id,
            "ticket_operator_username": ticket_op,
            "operator_project_id": None,
        },
    )
    return default_project_id


def _default_project_id() -> int | None:
    default = project_repository.get_by_slug("default")
    return default.id if default is not None else None


def _resolve_upload_project_id(
    *,
    operator_username: str,
    project_id: int | None,
    project_slug: str | None,
    short_id: str | None = None,
) -> int | None:
    """Resolve project_id for an operator upload.

    Precedence: explicit project_id > project_slug > operator's
    project_id (from the operators registry) > default project.
    """
    if project_id is not None:
        logger.info(
            "operator_upload_project_resolved",
            extra={
                "short_id": short_id,
                "operator_username": operator_username,
                "precedence_path": "explicit_id",
                "resolved_project_id": project_id,
            },
        )
        return project_id
    if project_slug:
        project = project_repository.get_by_slug(project_slug)
        if project is not None:
            logger.info(
                "operator_upload_project_resolved",
                extra={
                    "short_id": short_id,
                    "operator_username": operator_username,
                    "precedence_path": "explicit_slug",
                    "resolved_project_id": project.id,
                    "project_slug": project_slug,
                },
            )
            return project.id
    operator = operator_repository.find_by_username(operator_username)
    if operator is not None:
        logger.info(
            "operator_upload_project_resolved",
            extra={
                "short_id": short_id,
                "operator_username": operator_username,
                "precedence_path": "operator_default",
                "resolved_project_id": operator.project_id,
            },
        )
        return operator.project_id
    fallback = _default_project_id()
    logger.info(
        "operator_upload_project_resolved",
        extra={
            "short_id": short_id,
            "operator_username": operator_username,
            "precedence_path": "system_default",
            "resolved_project_id": fallback,
        },
    )
    return fallback


def _build_answer_context(
    *,
    chat_id: int | None,
    customer_username: str | None,
    trace_id: str,
    now: datetime,
) -> AnswerContext:
    return AnswerContext(
        chat_id=chat_id,
        customer_username=customer_username,
        trace_id=trace_id,
        now=now,
        language=_effective_default_language(),
        country_code=_effective_default_country(),
        timezone=_effective_default_timezone(),
        location=_effective_default_location(),
        grounding_threshold=_effective_grounding_threshold(),
        project_id=_resolve_inbound_project_id(chat_id),
    )


async def _safe_send_message(
    *, chat_id: int, text: str, failure_summary: str, failure_kind: str
) -> bool:
    try:
        await telegram_bot_sender.send_message(chat_id=chat_id, text=text)
        return True
    except Exception as exc:  # broad: ack/notify are best-effort
        incident = incident_repository.ingest(
            fingerprint="hitl_delivery_failures",
            severity="critical",
            summary=f"{failure_summary}: {exc}",
        )
        incident_repository.append_event(
            incident_id=incident.id,
            event_type=failure_kind,
            details=f"chat_id={chat_id};error={exc}",
        )
        return False


async def _notify_hitl_operator_summary(*, ticket_id: int, summary: str) -> bool:
    """Short-form operator DM, used for status changes like route/assign."""
    chat_id_raw = _effective_hitl_operator_chat_id()
    if not chat_id_raw:
        return False
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        return False
    try:
        await telegram_bot_sender.send_message(
            chat_id=chat_id,
            text=f"HITL ticket #{ticket_id}: {summary}",
        )
    except Exception:  # broad: best-effort notification
        return False
    return True


async def _notify_hitl_operator_with_question(
    *,
    ticket_id: int,
    question: str,
    customer_username: str | None,
) -> bool:
    chat_id_raw = _effective_hitl_operator_chat_id()
    if not chat_id_raw:
        logger.info(
            "hitl_operator_notified",
            extra={
                "ticket_id": ticket_id,
                "operator_chat_id": None,
                "dm_sent": False,
                "skip_reason": "no_operator_chat_id_configured",
            },
        )
        return False
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        logger.info(
            "hitl_operator_notified",
            extra={
                "ticket_id": ticket_id,
                "operator_chat_id": chat_id_raw,
                "dm_sent": False,
                "skip_reason": "operator_chat_id_invalid",
            },
        )
        return False
    customer_label = customer_username or "unknown"
    text = f"HITL ticket #{ticket_id} | from {customer_label} | {question}"
    sent = await _safe_send_message(
        chat_id=chat_id,
        text=text,
        failure_summary="HITL operator notification failed",
        failure_kind="hitl_operator_notify_failed",
    )
    logger.info(
        "hitl_operator_notified",
        extra={
            "ticket_id": ticket_id,
            "operator_chat_id": chat_id,
            "dm_sent": sent,
        },
    )
    return sent


def _persist_answer_trace(
    *,
    trace_id: str,
    request_text: str,
    response_mode: str,
    guardrail_outcome: str,
    guardrail_reasons: list[str],
    guardrail_score: float | None,
    retrieval: list[dict[str, object]],
    latency_ms: int,
    limitations: list[str],
    hitl_ticket_id: int | None = None,
) -> str | None:
    try:
        trace = answer_trace_repository.write(
            trace_id=trace_id,
            request_text=request_text,
            model_id=settings.openrouter_model,
            model_provider="openrouter",
            latency_ms=latency_ms,
            response_mode=response_mode,
            guardrails_applied=True,
            guardrail_outcome=guardrail_outcome,
            guardrail_reasons=guardrail_reasons,
            guardrail_score=guardrail_score,
            retrieval=retrieval,
            confidence=guardrail_score,
            limitations=limitations,
            hitl_ticket_id=hitl_ticket_id,
        )
    except Exception as exc:
        incident = incident_repository.ingest(
            fingerprint="answer_trace_persistence_failures",
            severity="critical",
            summary=f"Answer trace persistence failed: {exc}",
        )
        incident_repository.append_event(
            incident_id=incident.id,
            event_type="answer_trace_failed",
            details=f"trace_id={trace_id};error={exc}",
        )
        return None
    return trace.trace_id


def _resolve_calendar_escalation_assignee(calendar_operator: str | None) -> str:
    """Route a calendar escalation to the project's calendar operator.

    Falls back to the primary HITL operator when the configured calendar
    operator is missing/unregistered/inactive — escalation must always land
    somewhere, never drop.
    """
    primary = _effective_hitl_operator_username()
    if not calendar_operator:
        return primary
    operator = operator_repository.find_by_username(calendar_operator)
    if operator is not None and operator.is_active:
        return operator.username
    return primary


async def _escalate_calendar_availability(
    *,
    request: InboundMessageRequest,
    trace_id: str,
    latency_ms: int,
    metadata: dict[str, object],
) -> dict[str, object]:
    """HITL escalation for a calendar availability question, routed to the
    project's calendar operator with context."""
    calendar_operator = metadata.get("calendar_operator")
    escalation_context = metadata.get("escalation_context") or ""
    operator_username = _resolve_calendar_escalation_assignee(
        calendar_operator if isinstance(calendar_operator, str) else None
    )
    contextual_question = (
        f"[{escalation_context}] {request.text}"
        if escalation_context
        else request.text
    )

    active_ticket = (
        hitl_ticket_repository.find_active_for_chat(request.chat_id)
        if request.chat_id is not None
        else None
    )
    if active_ticket is not None:
        logger.info(
            "calendar_availability_escalation",
            extra={
                "trace_id": trace_id,
                "chat_id": request.chat_id,
                "calendar_operator": calendar_operator,
                "escalation_reason": metadata.get("reason"),
                "has_existing_ticket": True,
                "existing_ticket_id": active_ticket.id,
            },
        )
        await _notify_hitl_operator_with_question(
            ticket_id=active_ticket.id,
            question=f"[follow-up] {contextual_question}",
            customer_username=request.customer_username,
        )
        persisted_trace_id = _persist_answer_trace(
            trace_id=trace_id,
            request_text=request.text,
            response_mode="human_only",
            guardrail_outcome="escalated",
            guardrail_reasons=[],
            guardrail_score=None,
            retrieval=[],
            latency_ms=latency_ms,
            limitations=["awaiting_human_response", "coalesced_follow_up"],
            hitl_ticket_id=active_ticket.id,
        )
        return {
            "delivered": False,
            "escalated": True,
            "response_mode": "human_only",
            "hitl_ticket_id": active_ticket.id,
            "hitl_operator_username": (
                active_ticket.operator_username or operator_username
            ),
            "trace_id": persisted_trace_id,
            "coalesced": True,
        }

    ack_message = _effective_inbound_ack_message(
        project_id=_resolve_inbound_project_id(request.chat_id)
    )
    if request.chat_id is not None:
        await _safe_send_message(
            chat_id=request.chat_id,
            text=ack_message,
            failure_summary="Inbound ack delivery failed",
            failure_kind="inbound_ack_failed",
        )
    ticket = hitl_ticket_repository.create(
        conversation_ref=request.text[:120],
        reason="awaiting_human_response",
        target_chat_id=request.chat_id,
    )
    hitl_ticket_repository.assign(
        ticket_id=ticket.id,
        operator_username=operator_username,
    )
    logger.info(
        "calendar_availability_escalation",
        extra={
            "trace_id": trace_id,
            "chat_id": request.chat_id,
            "calendar_operator": calendar_operator,
            "escalation_reason": metadata.get("reason"),
            "has_existing_ticket": False,
            "ticket_id": ticket.id,
            "operator_username": operator_username,
        },
    )
    await _notify_hitl_operator_with_question(
        ticket_id=ticket.id,
        question=contextual_question,
        customer_username=request.customer_username,
    )
    persisted_trace_id = _persist_answer_trace(
        trace_id=trace_id,
        request_text=request.text,
        response_mode="human_only",
        guardrail_outcome="escalated",
        guardrail_reasons=[],
        guardrail_score=None,
        retrieval=[],
        latency_ms=latency_ms,
        limitations=["awaiting_human_response", "calendar_escalation"],
        hitl_ticket_id=ticket.id,
    )
    return {
        "delivered": False,
        "escalated": True,
        "response_mode": "human_only",
        "hitl_ticket_id": ticket.id,
        "hitl_operator_username": operator_username,
        "trace_id": persisted_trace_id,
    }


async def _dispatch_sales_escalation(
    *,
    request: InboundMessageRequest,
    trace_id: str,
    latency_ms: int,
    pipeline_result,
) -> dict[str, object]:
    """Deliver the sales fixed-line + open a HITL ticket (story 12.09 wiring).

    The sales answerer signals an escalation by returning
    ``handled=True`` with ``response_mode='sales_escalation'`` and a
    ``hitl_reason`` in its metadata. The customer receives the verbatim
    fallback line; the operator picks up via the resulting HITL ticket
    so unknown prices / drift / closing handoffs always reach a human.
    """
    metadata = pipeline_result.metadata
    answer_text = pipeline_result.text or ""
    hitl_reason = str(metadata.get("hitl_reason") or "sales_escalation")

    delivered = True
    if request.chat_id is not None:
        delivered = await _safe_send_message(
            chat_id=request.chat_id,
            text=answer_text,
            failure_summary="Inbound sales answer delivery failed",
            failure_kind="inbound_delivery_failed",
        )

    active_ticket = (
        hitl_ticket_repository.find_active_for_chat(request.chat_id)
        if request.chat_id is not None
        else None
    )
    if active_ticket is not None:
        await _notify_hitl_operator_with_question(
            ticket_id=active_ticket.id,
            question=f"[follow-up] {request.text}",
            customer_username=request.customer_username,
        )
        logger.info(
            "sales_escalation_coalesced",
            extra={
                "trace_id": trace_id,
                "chat_id": request.chat_id,
                "ticket_id": active_ticket.id,
                "hitl_reason": hitl_reason,
                "sales_turn_kind": metadata.get("sales_turn_kind"),
            },
        )
        persisted_trace_id = _persist_answer_trace(
            trace_id=trace_id,
            request_text=request.text,
            response_mode=pipeline_result.response_mode or "sales_escalation",
            guardrail_outcome="escalated",
            guardrail_reasons=[],
            guardrail_score=None,
            retrieval=[],
            latency_ms=latency_ms,
            limitations=["awaiting_human_response", "coalesced_sales_followup"],
            hitl_ticket_id=active_ticket.id,
        )
        return {
            "delivered": delivered,
            "escalated": True,
            "response_mode": pipeline_result.response_mode,
            "answer_text": answer_text,
            "answerer": metadata.get("answerer"),
            "hitl_ticket_id": active_ticket.id,
            "hitl_reason": hitl_reason,
            "trace_id": persisted_trace_id,
            "coalesced": True,
        }

    ticket = hitl_ticket_repository.create(
        conversation_ref=request.text[:120],
        reason=hitl_reason,
        target_chat_id=request.chat_id,
    )
    assignee = _pick_assignee_for_chat(request.chat_id)
    hitl_ticket_repository.assign(
        ticket_id=ticket.id,
        operator_username=assignee,
    )
    logger.info(
        "sales_escalation_ticket_created",
        extra={
            "trace_id": trace_id,
            "chat_id": request.chat_id,
            "ticket_id": ticket.id,
            "hitl_reason": hitl_reason,
            "operator_username": assignee,
            "sales_turn_kind": metadata.get("sales_turn_kind"),
        },
    )
    await _notify_hitl_operator_with_question(
        ticket_id=ticket.id,
        question=request.text,
        customer_username=request.customer_username,
    )
    persisted_trace_id = _persist_answer_trace(
        trace_id=trace_id,
        request_text=request.text,
        response_mode=pipeline_result.response_mode or "sales_escalation",
        guardrail_outcome="escalated",
        guardrail_reasons=[],
        guardrail_score=None,
        retrieval=[],
        latency_ms=latency_ms,
        limitations=["awaiting_human_response", "sales_escalation"],
        hitl_ticket_id=ticket.id,
    )
    return {
        "delivered": delivered,
        "escalated": True,
        "response_mode": pipeline_result.response_mode,
        "answer_text": answer_text,
        "answerer": metadata.get("answerer"),
        "hitl_ticket_id": ticket.id,
        "hitl_operator_username": assignee,
        "hitl_reason": hitl_reason,
        "trace_id": persisted_trace_id,
    }


@app.post("/conversations/inbound")
async def conversations_inbound(request: InboundMessageRequest) -> dict[str, object]:
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="empty_text")

    started_at = time.perf_counter()
    trace_id = request.trace_id or str(uuid.uuid4())

    logger.info(
        "inbound_received",
        extra={
            "trace_id": trace_id,
            "chat_id": request.chat_id,
            "customer_username": request.customer_username,
            "text_length": len(request.text),
            "text": request.text,
        },
    )

    # Idempotency: if we've already processed this trace_id, return the cached
    # outcome and skip the side effects (ack, ticket, operator notify). This
    # defends against duplicate /conversations/inbound calls — for example a
    # Telegram webhook retry that slips past the bot_gateway dedup, or a
    # script that posts the same trace_id twice.
    existing_trace = answer_trace_repository.find_by_trace_id(trace_id)
    if existing_trace is not None:
        logger.info(
            "inbound_idempotent_replay",
            extra={
                "trace_id": trace_id,
                "response_mode": existing_trace.response_mode,
                "hitl_ticket_id": existing_trace.hitl_ticket_id,
            },
        )
        return {
            "deduplicated": True,
            "delivered": False,
            "escalated": existing_trace.response_mode == "human_only",
            "response_mode": existing_trace.response_mode,
            "hitl_ticket_id": existing_trace.hitl_ticket_id,
            "trace_id": existing_trace.trace_id,
        }

    now = datetime.now(UTC)
    # Story 12.08: cancel any pending +1d nudge BEFORE the pipeline runs so a
    # reply arriving at the same instant the queue fires never double-notifies.
    # The hook is a no-op when chat_id is None.
    await asyncio.to_thread(
        maybe_cancel,
        repo=sales_followup_repository,
        chat_id=request.chat_id,
        now=now,
        trace_id=trace_id,
    )
    ctx = _build_answer_context(
        chat_id=request.chat_id,
        customer_username=request.customer_username,
        trace_id=trace_id,
        now=now,
    )

    pipeline_result = await answer_pipeline.run(question=request.text, ctx=ctx)
    latency_ms = int((time.perf_counter() - started_at) * 1000)

    if (
        pipeline_result.handled
        and pipeline_result.response_mode == RESPONSE_MODE_ESCALATION
    ):
        # Calendar owns this question but couldn't answer confidently (provider
        # / token failure or twice-ambiguous). Escalate to a human routed to the
        # project's calendar operator — never deliver a fabricated answer.
        return await _escalate_calendar_availability(
            request=request,
            trace_id=trace_id,
            latency_ms=latency_ms,
            metadata=pipeline_result.metadata,
        )

    if (
        pipeline_result.handled
        and pipeline_result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    ):
        # Sales answerer wants to deliver a fixed customer-facing line AND open
        # a HITL ticket so the operator picks up the unknown price / drift /
        # closing handoff. We deliver the line first, then create+notify the
        # ticket using ``hitl_reason`` from the answerer metadata.
        return await _dispatch_sales_escalation(
            request=request,
            trace_id=trace_id,
            latency_ms=latency_ms,
            pipeline_result=pipeline_result,
        )

    if pipeline_result.handled:
        retrieval = pipeline_result.metadata.get("retrieval") or []
        guardrail_score = pipeline_result.metadata.get("guardrail_score")
        delivered = True
        if request.chat_id is not None:
            delivered = await _safe_send_message(
                chat_id=request.chat_id,
                text=pipeline_result.text or "",
                failure_summary="Inbound answer delivery failed",
                failure_kind="inbound_delivery_failed",
            )
        limitations: list[str] = [] if retrieval else ["no_retrieval"]
        persisted_trace_id = _persist_answer_trace(
            trace_id=trace_id,
            request_text=request.text,
            response_mode=pipeline_result.response_mode or "unknown",
            guardrail_outcome="valid",
            guardrail_reasons=[],
            guardrail_score=(
                float(guardrail_score) if guardrail_score is not None else None
            ),
            retrieval=list(retrieval),
            latency_ms=latency_ms,
            limitations=limitations,
        )
        return {
            "delivered": delivered,
            "escalated": False,
            "response_mode": pipeline_result.response_mode,
            "answer_text": pipeline_result.text,
            "answerer": pipeline_result.metadata.get("answerer"),
            "trace_id": persisted_trace_id,
        }

    # Escalation path. Coalesce onto an active ticket for the same chat when
    # one exists so a customer's rapid follow-up questions become one human
    # conversation instead of N parallel tickets + N acks.
    active_ticket = (
        hitl_ticket_repository.find_active_for_chat(request.chat_id)
        if request.chat_id is not None
        else None
    )
    if active_ticket is not None:
        # Customer already has an active ticket. Don't re-ack (they got one
        # on the original message); just forward the follow-up to the
        # assigned operator as a continuation.
        operator_username = (
            active_ticket.operator_username or _effective_hitl_operator_username()
        )
        logger.info(
            "hitl_escalation_start",
            extra={
                "trace_id": trace_id,
                "chat_id": request.chat_id,
                "customer_username": request.customer_username,
                "has_existing_ticket": True,
                "existing_ticket_id": active_ticket.id,
                "ticket_operator_username": operator_username,
                "is_follow_up": True,
            },
        )
        await _notify_hitl_operator_with_question(
            ticket_id=active_ticket.id,
            question=f"[follow-up] {request.text}",
            customer_username=request.customer_username,
        )
        persisted_trace_id = _persist_answer_trace(
            trace_id=trace_id,
            request_text=request.text,
            response_mode="human_only",
            guardrail_outcome="escalated",
            guardrail_reasons=[],
            guardrail_score=None,
            retrieval=[],
            latency_ms=latency_ms,
            limitations=["awaiting_human_response", "coalesced_follow_up"],
            hitl_ticket_id=active_ticket.id,
        )
        return {
            "delivered": False,
            "escalated": True,
            "response_mode": "human_only",
            "hitl_ticket_id": active_ticket.id,
            "hitl_operator_username": operator_username,
            "trace_id": persisted_trace_id,
            "coalesced": True,
        }

    ack_message = _effective_inbound_ack_message(project_id=ctx.project_id)
    logger.info(
        "hitl_escalation_start",
        extra={
            "trace_id": trace_id,
            "chat_id": request.chat_id,
            "customer_username": request.customer_username,
            "has_existing_ticket": False,
            "existing_ticket_id": None,
            "is_follow_up": False,
        },
    )
    if request.chat_id is not None:
        await _safe_send_message(
            chat_id=request.chat_id,
            text=ack_message,
            failure_summary="Inbound ack delivery failed",
            failure_kind="inbound_ack_failed",
        )

    ticket = hitl_ticket_repository.create(
        conversation_ref=request.text[:120],
        reason="awaiting_human_response",
        target_chat_id=request.chat_id,
    )
    assignee = _pick_assignee_for_chat(request.chat_id)
    hitl_ticket_repository.assign(
        ticket_id=ticket.id,
        operator_username=assignee,
    )
    logger.info(
        "hitl_ticket_created",
        extra={
            "trace_id": trace_id,
            "ticket_id": ticket.id,
            "operator_username": assignee,
            "reason": "awaiting_human_response",
            "conversation_ref_snippet": request.text[:120],
        },
    )
    await _notify_hitl_operator_with_question(
        ticket_id=ticket.id,
        question=request.text,
        customer_username=request.customer_username,
    )

    persisted_trace_id = _persist_answer_trace(
        trace_id=trace_id,
        request_text=request.text,
        response_mode="human_only",
        guardrail_outcome="escalated",
        guardrail_reasons=[],
        guardrail_score=None,
        retrieval=[],
        latency_ms=latency_ms,
        limitations=["awaiting_human_response"],
        hitl_ticket_id=ticket.id,
    )
    return {
        "delivered": False,
        "escalated": True,
        "response_mode": "human_only",
        "hitl_ticket_id": ticket.id,
        "hitl_operator_username": _effective_hitl_operator_username(),
        "trace_id": persisted_trace_id,
    }


class FollowupRescheduleRequest(BaseModel):
    new_fire_at: datetime


def _followup_row_to_dict(
    row: FollowupRow, *, intent_dates: str | None = None
) -> dict[str, object]:
    return {
        "id": row.id,
        "chat_id": row.chat_id,
        "project_id": row.project_id,
        "fire_at": row.fire_at.isoformat(),
        "status": row.status,
        "reason": row.reason,
        "intent_dates": intent_dates,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _intent_dates_for_chat(chat_id: int) -> str | None:
    state = sales_state_repository.get(chat_id)
    if state is None:
        return None
    collected = state.get("collected_intent") or {}
    raw = collected.get("dates")
    return str(raw) if isinstance(raw, str) and raw.strip() else None


@app.get("/sales/followups/due")
def list_due_followups(
    now: str | None = None,
    _: Annotated[str, Depends(require_internal_token)] = "",
) -> dict[str, object]:
    if now is None:
        cursor = datetime.now(UTC)
    else:
        try:
            parsed = datetime.fromisoformat(now)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_now") from exc
        cursor = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    rows = sales_followup_repository.due(now=cursor, limit=100)
    return {
        "rows": [
            _followup_row_to_dict(
                row, intent_dates=_intent_dates_for_chat(row.chat_id)
            )
            for row in rows
        ]
    }


@app.post("/sales/followups/{followup_id}/skip-stale")
def skip_stale_followup(
    followup_id: int,
    _: Annotated[str, Depends(require_internal_token)] = "",
) -> dict[str, object]:
    row = sales_followup_repository.get(followup_id)
    if row is None:
        raise HTTPException(status_code=404, detail="followup_not_found")
    sales_followup_repository.mark_skipped_stale(
        followup_id, reason=REASON_PAST_INTENT_DATE, now=datetime.now(UTC)
    )
    return {"ok": True}


@app.post("/sales/followups/{followup_id}/reschedule")
def reschedule_followup(
    followup_id: int,
    payload: FollowupRescheduleRequest,
    _: Annotated[str, Depends(require_internal_token)] = "",
) -> dict[str, object]:
    row = sales_followup_repository.get(followup_id)
    if row is None:
        raise HTTPException(status_code=404, detail="followup_not_found")
    fire_at = payload.new_fire_at
    if fire_at.tzinfo is None:
        fire_at = fire_at.replace(tzinfo=UTC)
    sales_followup_repository.reschedule(
        followup_id, new_fire_at=fire_at, now=datetime.now(UTC)
    )
    return {"ok": True}


@app.post("/sales/followups/{followup_id}/fire")
async def fire_followup(
    followup_id: int,
    _: Annotated[str, Depends(require_internal_token)] = "",
) -> dict[str, object]:
    row = sales_followup_repository.get(followup_id)
    if row is None:
        raise HTTPException(status_code=404, detail="followup_not_found")
    outcome = await sales_followup_fire_handler.fire(row)
    return {
        "ok": True,
        "sent": outcome.sent,
        "fallback_text_used": outcome.fallback_text_used,
    }


@app.post("/sales/_dev/tick-followup-now")
async def dev_tick_followup_now() -> dict[str, object]:
    """Dev-only fast-forward: fire every currently-due follow-up row.

    Story 12.09 uses this in ``scripts/epic12_signoff.sh`` to drive the
    proactive nudge without manipulating the system clock. The endpoint
    is exposed ONLY when ``Settings.app_env == "dev"``; any other env
    returns 404 so a misconfigured production never accidentally
    exposes an unauthenticated tick endpoint.
    """
    if settings.app_env != "dev":
        raise HTTPException(status_code=404, detail="not_found")
    rows = sales_followup_repository.due(now=datetime.now(UTC), limit=100)
    fired = 0
    for row in rows:
        outcome = await sales_followup_fire_handler.fire(row)
        if outcome.sent or outcome.fallback_text_used:
            fired += 1
    return {"fired": fired}


class SalesAnalyzeKbFileRequest(BaseModel):
    project_id: int
    operator_file_short_id: str
    now: str | None = None


def _analysis_outcome_to_dict(outcome: AnalysisOutcome) -> dict[str, object]:
    return {
        "registered": outcome.registered,
        "material_id": outcome.material_id,
        "reason": outcome.reason,
    }


class SalesServiceCreateRequest(BaseModel):
    project_id: int
    name: str
    description_md: str | None = None
    tags: list[str] | None = None


def _sales_service_to_dict(service) -> dict[str, object]:
    return {
        "id": service.id,
        "project_id": service.project_id,
        "name": service.name,
        "description_md": service.description_md,
        "tags": service.tags,
        "is_active": service.is_active,
    }


def _sales_state_row_to_dict(row: dict) -> dict[str, object]:
    """Curated projection of a `StateRepository` row for the API response.

    Explicitly enumerates only the keys we want on the wire so a future
    column added to the row dict (e.g. a token-bearing field) cannot
    silently leak through.
    """
    last_customer = row.get("last_customer_msg_at")
    last_bot = row.get("last_bot_msg_at")
    return {
        "chat_id": row["chat_id"],
        "project_id": row["project_id"],
        "current_stage": row["current_stage"],
        "collected_intent": row.get("collected_intent") or {},
        "last_proposal": row.get("last_proposal"),
        "last_customer_msg_at": (
            last_customer.isoformat() if last_customer is not None else None
        ),
        "last_bot_msg_at": (
            last_bot.isoformat() if last_bot is not None else None
        ),
    }


@app.post("/sales/services")
async def sales_services_add(
    request: SalesServiceCreateRequest,
    _: Annotated[str, Depends(require_internal_token)] = "",
) -> dict[str, object]:
    clean_name = (request.name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="invalid_service_name")
    try:
        service_id = await asyncio.to_thread(
            sales_services_repository.add,
            project_id=request.project_id,
            name=clean_name,
            description_md=request.description_md,
            tags=request.tags,
            now=datetime.now(UTC),
        )
    except ServiceAlreadyExists as exc:
        raise HTTPException(
            status_code=409, detail="service_already_exists"
        ) from exc
    return {"id": service_id}


@app.get("/sales/services")
async def sales_services_list(
    project_id: int,
    _: Annotated[str, Depends(require_internal_token)] = "",
) -> dict[str, object]:
    services = await asyncio.to_thread(
        sales_services_repository.list_active, project_id=project_id
    )
    return {"services": [_sales_service_to_dict(s) for s in services]}


@app.delete("/sales/services/{service_id}")
async def sales_services_delete(
    service_id: int,
    _: Annotated[str, Depends(require_internal_token)] = "",
) -> dict[str, object]:
    try:
        await asyncio.to_thread(
            sales_services_repository.soft_delete, service_id=service_id
        )
    except ServiceNotFound as exc:
        raise HTTPException(status_code=404, detail="service_not_found") from exc
    return {"ok": True}


@app.get("/sales/state")
async def sales_state_list(
    project_id: int,
    chat_id: int | None = None,
    _: Annotated[str, Depends(require_internal_token)] = "",
) -> dict[str, object]:
    rows = await asyncio.to_thread(
        sales_state_repository.list_active,
        project_id=project_id,
        chat_id=chat_id,
    )
    return {"states": [_sales_state_row_to_dict(row) for row in rows]}


@app.post("/sales/materials/analyze-kb-file")
async def sales_materials_analyze_kb_file(
    request: SalesAnalyzeKbFileRequest,
    _: Annotated[str, Depends(require_internal_token)] = "",
) -> dict[str, object]:
    """Run the 12.05b client-materials analyzer on a KB-uploaded file.

    Called by the bot_gateway KB-upload hook after a successful KB ingest.
    The endpoint always returns a 200 with the ``AnalysisOutcome`` shape —
    the bot treats ``registered=False`` as "no extra message" and never
    surfaces an error from this endpoint. The injected ``now`` is tz-aware
    UTC; tests may pass an explicit ISO string for determinism.
    """
    parsed_now = _parse_optional_now(request.now)
    effective_now = parsed_now if parsed_now is not None else datetime.now(UTC)
    outcome = await client_materials_analyzer.analyze_and_register(
        project_id=request.project_id,
        operator_file_short_id=request.operator_file_short_id,
        now=effective_now,
    )
    return _analysis_outcome_to_dict(outcome)


def _extraction_outcome_to_dict(
    outcome: ExtractionOutcome,
) -> dict[str, object]:
    return {
        "added": [
            {"service_id": item.service_id, "name": item.name}
            for item in outcome.added
        ],
        "skipped_existing": list(outcome.skipped_existing),
        "reason": outcome.reason,
    }


@app.post("/sales/services/extract-from-kb-file")
async def sales_services_extract_from_kb_file(
    request: SalesAnalyzeKbFileRequest,
    _: Annotated[str, Depends(require_internal_token)] = "",
) -> dict[str, object]:
    """Run the 12.05c services extractor on a KB-uploaded file.

    Called by the bot_gateway KB-upload hook in parallel with the 12.05b
    materials analyzer. The endpoint always returns a 200 with the
    ``ExtractionOutcome`` shape — the bot treats an empty ``added`` list
    as "no extra message" and never surfaces an error from this endpoint.
    The injected ``now`` is tz-aware UTC; tests may pass an explicit ISO
    string for determinism.
    """
    parsed_now = _parse_optional_now(request.now)
    effective_now = parsed_now if parsed_now is not None else datetime.now(UTC)
    outcome = await services_extractor.extract_and_register(
        project_id=request.project_id,
        operator_file_short_id=request.operator_file_short_id,
        now=effective_now,
    )
    return _extraction_outcome_to_dict(outcome)


_MATERIAL_CAPTION_MAX_CHARS = 200
_MATERIAL_ALLOWED_KINDS: frozenset[str] = frozenset(
    {"video", "photo", "pdf", "document"}
)


class SalesMaterialCreateRequest(BaseModel):
    project_id: int
    kind: str
    local_path: str
    byte_size: int
    duration_seconds: int | None = None
    caption: str | None = None
    tags: list[str] | None = None
    telegram_file_id: str | None = None
    source_operator_file_id: str | None = None


class SalesMaterialDispatchRequest(BaseModel):
    chat_id: int
    material_id: int
    caption_override: str | None = None
    trace_id: str | None = None


def _client_material_to_dict(row: ClientMaterial) -> dict[str, object]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "kind": row.kind,
        "local_path": row.local_path,
        "byte_size": row.byte_size,
        "duration_seconds": row.duration_seconds,
        "caption": row.caption,
        "tags": row.tags,
        "telegram_file_id": row.telegram_file_id,
        "source_operator_file_id": row.source_operator_file_id,
        "is_active": row.is_active,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _validate_material_caption(caption: str | None) -> None:
    if caption is not None and len(caption) > _MATERIAL_CAPTION_MAX_CHARS:
        raise HTTPException(status_code=400, detail="caption_too_long")


@app.post("/sales/materials")
async def sales_materials_add(
    request: SalesMaterialCreateRequest,
    _: Annotated[str, Depends(require_internal_token)] = "",
) -> dict[str, object]:
    if request.kind not in _MATERIAL_ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail="invalid_kind")
    _validate_material_caption(request.caption)
    material_id = await asyncio.to_thread(
        client_materials_repository.add,
        project_id=request.project_id,
        kind=request.kind,
        local_path=request.local_path,
        byte_size=request.byte_size,
        duration_seconds=request.duration_seconds,
        caption=request.caption,
        tags=request.tags,
        telegram_file_id=request.telegram_file_id,
        source_operator_file_id=request.source_operator_file_id,
        now=datetime.now(UTC),
    )
    return {"id": material_id}


@app.get("/sales/materials")
async def sales_materials_list(
    project_id: int,
    _: Annotated[str, Depends(require_internal_token)] = "",
) -> dict[str, object]:
    rows = await asyncio.to_thread(
        client_materials_repository.list_active, project_id=project_id
    )
    return {"materials": [_client_material_to_dict(row) for row in rows]}


@app.delete("/sales/materials/{material_id}")
async def sales_materials_delete(
    material_id: int,
    _: Annotated[str, Depends(require_internal_token)] = "",
) -> dict[str, object]:
    row = await asyncio.to_thread(
        client_materials_repository.get, material_id=material_id
    )
    if row is None or not row.is_active:
        raise HTTPException(status_code=404, detail="material_not_found")
    await asyncio.to_thread(
        client_materials_repository.soft_delete, material_id=material_id
    )
    return {"ok": True}


_DISPATCH_METHOD_BY_KIND: dict[str, str] = {
    "video": "send_video",
    "photo": "send_photo",
    "pdf": "send_document",
    "document": "send_document",
}


@app.post("/sales/dispatch/material")
async def sales_dispatch_material(
    request: SalesMaterialDispatchRequest,
    _: Annotated[str, Depends(require_internal_token)] = "",
) -> dict[str, object]:
    """Send a registered client material to a Telegram chat.

    Prefers the cached ``telegram_file_id`` so subsequent sends never read
    from disk; on a fresh upload the returned ``file_id`` is cached on the
    row via :meth:`ClientMaterialsRepository.update_telegram_file_id`.

    Telegram-side failures resolve to ``{ok: false, error_reason}`` so the
    caller (the answerer's media moment) can fall back to a textual reply
    within the same turn. The ``telegram_file_id`` is never logged.
    """
    _validate_material_caption(request.caption_override)
    row = await asyncio.to_thread(
        client_materials_repository.get, material_id=request.material_id
    )
    if row is None or not row.is_active:
        raise HTTPException(status_code=404, detail="material_not_found")

    method_name = _DISPATCH_METHOD_BY_KIND.get(row.kind)
    if method_name is None:
        raise HTTPException(status_code=400, detail="invalid_kind")

    caption = (
        request.caption_override
        if request.caption_override is not None
        else row.caption
    )
    send_method = getattr(telegram_bot_sender, method_name)
    has_cached_id = row.telegram_file_id is not None
    kwargs: dict[str, object] = {"chat_id": request.chat_id}
    if caption is not None:
        kwargs["caption"] = caption
    if has_cached_id:
        kwargs["file_id"] = row.telegram_file_id
    else:
        kwargs["local_path"] = Path(row.local_path)

    try:
        result = await send_method(**kwargs)
    except TelegramMediaSendError as exc:
        # NOTE: never log ``exc.description`` — Telegram error descriptions can
        # echo the ``file_id`` back to us, which would violate the Story 12.05
        # 'never log telegram_file_id' exit criterion.
        logger.warning(
            "sales_material_dispatch_failed",
            extra={
                "trace_id": request.trace_id,
                "material_id": row.id,
                "kind": row.kind,
                "reason": exc.reason,
                "cached_file_id_used": has_cached_id,
            },
        )
        return {"ok": False, "error_reason": exc.reason}

    returned_file_id = result.get("telegram_file_id")
    if not has_cached_id and isinstance(returned_file_id, str) and returned_file_id:
        await asyncio.to_thread(
            client_materials_repository.update_telegram_file_id,
            material_id=row.id,
            telegram_file_id=returned_file_id,
        )
        cached_now = True
    else:
        cached_now = False
    logger.info(
        "sales_material_dispatched",
        extra={
            "trace_id": request.trace_id,
            "material_id": row.id,
            "kind": row.kind,
            "telegram_file_id_cached_now": cached_now,
            "used_cached_file_id": has_cached_id,
        },
    )
    return {
        "ok": True,
        "telegram_file_id_cached": has_cached_id,
    }


@app.post("/rag/ingest")
def ingest_rag(request: RagIngestRequest) -> dict[str, object]:
    try:
        inserted = rag_repository.ingest(source_id=request.source_id, text=request.text)
    except Exception as exc:
        incident = incident_repository.ingest(
            fingerprint="rag_ingest_failures",
            severity="critical",
            summary=f"RAG ingest failed: {exc}",
        )
        incident_repository.append_event(
            incident_id=incident.id,
            event_type="rag_ingest_failed",
            details=f"source_id={request.source_id}",
        )
        raise HTTPException(status_code=500, detail="rag_ingest_failed") from exc

    return {"source_id": request.source_id, "inserted_chunks": inserted}


@app.post("/rag/retrieve")
def retrieve_rag(request: RagRetrieveRequest) -> dict[str, object]:
    chunks = rag_repository.retrieve(
        query=request.query,
        limit=request.limit,
        project_id=request.project_id,
    )
    return {
        "items": [
            {
                "id": chunk.id,
                "source_id": chunk.source_id,
                "chunk_text": chunk.chunk_text,
                "score": chunk.score,
                "project_id": chunk.project_id,
                "is_confidential": chunk.is_confidential,
            }
            for chunk in chunks
        ]
    }


@app.post("/incidents/events")
async def ingest_incident_event(request: IncidentEventRequest) -> dict[str, object]:
    incident = incident_repository.ingest(
        fingerprint=request.fingerprint,
        severity=request.severity,
        summary=request.summary,
    )
    sent = False
    delivery_status = "not_critical"
    if telegram_notifier.is_critical_event(
        fingerprint=request.fingerprint,
        severity=request.severity,
    ):
        last_sent_at = incident_repository.get_last_telegram_sent_at(incident.id)
        if last_sent_at is not None:
            elapsed = (datetime.now(UTC) - last_sent_at).total_seconds()
            if elapsed < settings.telegram_alert_debounce_seconds:
                delivery_status = "debounced"
            else:
                sent, delivery_status = await telegram_notifier.notify_if_critical(
                    incident_id=incident.id,
                    fingerprint=request.fingerprint,
                    severity=request.severity,
                    summary=request.summary,
                    occurrence_count=incident.occurrence_count,
                )
        else:
            sent, delivery_status = await telegram_notifier.notify_if_critical(
                incident_id=incident.id,
                fingerprint=request.fingerprint,
                severity=request.severity,
                summary=request.summary,
                occurrence_count=incident.occurrence_count,
            )
    incident_repository.append_event(
        incident_id=incident.id,
        event_type="telegram_notify",
        details=f"status={delivery_status}",
    )
    return {
        "id": incident.id,
        "fingerprint": incident.fingerprint,
        "status": incident.status,
        "occurrence_count": incident.occurrence_count,
        "telegram_notification_sent": sent,
        "telegram_delivery_status": delivery_status,
    }


@app.get("/incidents/{fingerprint}")
def get_incidents_by_fingerprint(fingerprint: str) -> dict[str, object]:
    incidents = incident_repository.get_by_fingerprint(fingerprint)
    return {
        "fingerprint": fingerprint,
        "items": [
            {
                "id": incident.id,
                "status": incident.status,
                "is_read": incident.is_read,
                "severity": incident.severity,
                "summary": incident.summary,
                "occurrence_count": incident.occurrence_count,
                "first_seen_at": incident.first_seen_at,
                "last_seen_at": incident.last_seen_at,
                "acknowledged_at": incident.acknowledged_at,
                "resolved_at": incident.resolved_at,
            }
            for incident in incidents
        ],
    }


@app.get("/incidents")
def list_incidents() -> dict[str, object]:
    incidents = incident_repository.list_incidents()
    return {
        "items": [
            {
                "id": incident.id,
                "fingerprint": incident.fingerprint,
                "status": incident.status,
                "is_read": incident.is_read,
                "severity": incident.severity,
                "summary": incident.summary,
                "occurrence_count": incident.occurrence_count,
                "first_seen_at": incident.first_seen_at,
                "last_seen_at": incident.last_seen_at,
                "acknowledged_at": incident.acknowledged_at,
                "resolved_at": incident.resolved_at,
            }
            for incident in incidents
        ]
    }


@app.post("/incidents/{incident_id}/read")
def mark_incident_read(incident_id: int) -> dict[str, object]:
    incident = incident_repository.mark_read(incident_id)
    return {"id": incident.id, "status": incident.status, "is_read": incident.is_read}


@app.post("/incidents/{incident_id}/ack")
def acknowledge_incident(incident_id: int) -> dict[str, object]:
    incident = incident_repository.acknowledge(incident_id)
    return {
        "id": incident.id,
        "status": incident.status,
        "is_read": incident.is_read,
        "acknowledged_at": incident.acknowledged_at,
    }


@app.post("/incidents/{incident_id}/resolve")
def resolve_incident(incident_id: int) -> dict[str, object]:
    incident = incident_repository.resolve(incident_id)
    return {
        "id": incident.id,
        "status": incident.status,
        "is_read": incident.is_read,
        "resolved_at": incident.resolved_at,
    }


@app.get("/incidents/{incident_id}/timeline")
def get_incident_timeline(incident_id: int) -> dict[str, object]:
    timeline = incident_repository.get_timeline(incident_id)
    return {
        "incident_id": incident_id,
        "events": [
            {
                "id": event.id,
                "event_type": event.event_type,
                "details": event.details,
                "created_at": event.created_at,
            }
            for event in timeline
        ],
    }


@app.get("/hitl/tickets")
def list_hitl_tickets() -> dict[str, object]:
    tickets = hitl_ticket_repository.list_all()
    return {
        "items": [
            {
                "id": ticket.id,
                "conversation_ref": ticket.conversation_ref,
                "reason": ticket.reason,
                "status": ticket.status,
                "operator_username": ticket.operator_username,
                "target_chat_id": ticket.target_chat_id,
                "created_at": ticket.created_at,
                "updated_at": ticket.updated_at,
                "resolved_at": ticket.resolved_at,
            }
            for ticket in tickets
        ]
    }


@app.post("/hitl/tickets/{ticket_id}/route")
async def route_hitl_ticket(ticket_id: int, request: HitlRouteRequest) -> dict[str, object]:
    operator = request.operator_username or _effective_hitl_operator_username()
    if not operator:
        incident = incident_repository.ingest(
            fingerprint="hitl_delivery_failures",
            severity="critical",
            summary="HITL ticket routing failed: missing operator username",
        )
        incident_repository.append_event(
            incident_id=incident.id,
            event_type="hitl_route_failed",
            details=f"ticket_id={ticket_id}",
        )
        raise HTTPException(status_code=503, detail="hitl_operator_missing")

    ticket = hitl_ticket_repository.assign(ticket_id=ticket_id, operator_username=operator)
    await _notify_hitl_operator_summary(
        ticket_id=ticket.id, summary=f"assigned to {operator}"
    )
    return {
        "id": ticket.id,
        "status": ticket.status,
        "operator_username": ticket.operator_username,
    }


@app.post("/hitl/tickets/{ticket_id}/resolve")
def resolve_hitl_ticket(ticket_id: int) -> dict[str, object]:
    ticket = hitl_ticket_repository.resolve(ticket_id=ticket_id)
    return {
        "id": ticket.id,
        "status": ticket.status,
        "resolved_at": ticket.resolved_at,
    }


@app.post("/hitl/runtime-config/persona")
async def update_bot_persona(request: BotPersonaRequest) -> dict[str, object]:
    effective_operator = (
        hitl_ticket_repository.get_runtime_config("hitl_primary_operator_username")
        or settings.hitl_primary_operator_username
    )
    if request.updated_by != effective_operator:
        raise HTTPException(status_code=403, detail="not_authorized")

    first_name = _validate_persona_name(request.first_name)
    # Last name is optional: callers can rename the bot to a single given name
    # ("Анна") without inventing a surname. Empty / whitespace-only values are
    # accepted and stored as an empty string; a present non-empty value is
    # validated by the same regex as the first name.
    last_name_raw = request.last_name.strip() if request.last_name else ""
    last_name = _validate_persona_name(last_name_raw) if last_name_raw else ""

    description: str | None = None
    if request.description is not None:
        candidate = request.description.strip()
        if not candidate or len(candidate) > _PERSONA_DESCRIPTION_MAX:
            raise HTTPException(status_code=422, detail="invalid_description")
        description = candidate

    short_description: str | None = None
    if request.short_description is not None:
        candidate = request.short_description.strip()
        if not candidate or len(candidate) > _PERSONA_SHORT_DESCRIPTION_MAX:
            raise HTTPException(status_code=422, detail="invalid_short_description")
        short_description = candidate

    hitl_ticket_repository.set_runtime_config(
        key="bot_persona_first_name",
        value=first_name,
        updated_by=request.updated_by,
    )
    hitl_ticket_repository.set_runtime_config(
        key="bot_persona_last_name",
        value=last_name,
        updated_by=request.updated_by,
    )
    if description is not None:
        hitl_ticket_repository.set_runtime_config(
            key="bot_telegram_description",
            value=description,
            updated_by=request.updated_by,
        )
    if short_description is not None:
        hitl_ticket_repository.set_runtime_config(
            key="bot_telegram_short_description",
            value=short_description,
            updated_by=request.updated_by,
        )

    full_name = f"{first_name} {last_name}".strip()
    telegram_results: dict[str, object] = {}
    telegram_results["set_my_name"] = await _safe_telegram_identity_call(
        method=telegram_bot_sender.set_my_name, name=full_name
    )
    effective_description = description or (
        hitl_ticket_repository.get_runtime_config("bot_telegram_description")
        or settings.bot_telegram_description
    )
    telegram_results["set_my_description"] = await _safe_telegram_identity_call(
        method=telegram_bot_sender.set_my_description,
        description=effective_description,
    )
    effective_short = short_description or (
        hitl_ticket_repository.get_runtime_config("bot_telegram_short_description")
        or settings.bot_telegram_short_description
    )
    telegram_results["set_my_short_description"] = (
        await _safe_telegram_identity_call(
            method=telegram_bot_sender.set_my_short_description,
            short_description=effective_short,
        )
    )

    return {
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "telegram": telegram_results,
    }


async def _safe_telegram_identity_call(*, method, **kwargs) -> dict:
    """Wrap a single setMyX call so a Telegram error doesn't fail the endpoint."""
    try:
        return await method(**kwargs)
    except Exception as exc:  # broad: identity calls are best-effort
        logger.warning(
            "telegram_identity_call_failed",
            extra={"method": method.__name__, "error": str(exc)},
        )
        return {"ok": False, "error": str(exc)}


@app.on_event("startup")
async def sync_telegram_identity_on_startup() -> None:
    """Push persona/description from config to Telegram once on boot.

    Idempotent on Telegram's side; safe to run on every restart. Skips
    entirely when the bot token is the unconfigured placeholder so unit
    tests don't reach the network.
    """
    if not telegram_bot_sender._is_token_configured():
        return
    first_name, last_name = _effective_bot_persona()
    description = (
        hitl_ticket_repository.get_runtime_config("bot_telegram_description")
        or settings.bot_telegram_description
    )
    short_description = (
        hitl_ticket_repository.get_runtime_config("bot_telegram_short_description")
        or settings.bot_telegram_short_description
    )
    await _safe_telegram_identity_call(
        method=telegram_bot_sender.set_my_name,
        name=f"{first_name} {last_name}".strip(),
    )
    await _safe_telegram_identity_call(
        method=telegram_bot_sender.set_my_description, description=description
    )
    await _safe_telegram_identity_call(
        method=telegram_bot_sender.set_my_short_description,
        short_description=short_description,
    )


@app.post("/hitl/tickets/{ticket_id}/reply")
async def deliver_hitl_ticket_reply(ticket_id: int, request: HitlReplyRequest) -> dict[str, object]:
    ticket = hitl_ticket_repository.get(ticket_id)
    if ticket.operator_username != request.operator_username:
        raise HTTPException(status_code=403, detail="operator_not_assigned")
    if not request.reply_text.strip():
        raise HTTPException(status_code=400, detail="empty_reply")
    if ticket.target_chat_id is None:
        incident = incident_repository.ingest(
            fingerprint="hitl_delivery_failures",
            severity="critical",
            summary="HITL reply delivery failed: missing target chat id",
        )
        incident_repository.append_event(
            incident_id=incident.id,
            event_type="hitl_delivery_failed",
            details=f"ticket_id={ticket_id};reason=missing_chat_id",
        )
        raise HTTPException(status_code=503, detail="missing_target_chat_id")

    try:
        # Delivers only the operator-authored body as bot text.
        message_id = await telegram_bot_sender.send_message(
            chat_id=ticket.target_chat_id,
            text=request.reply_text.strip(),
        )
    except RuntimeError as exc:
        incident = incident_repository.ingest(
            fingerprint="hitl_delivery_failures",
            severity="critical",
            summary=f"HITL reply delivery failed: {exc}",
        )
        incident_repository.append_event(
            incident_id=incident.id,
            event_type="hitl_delivery_failed",
            details=f"ticket_id={ticket_id};reason=missing_bot_token",
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - provider failure path
        incident = incident_repository.ingest(
            fingerprint="hitl_delivery_failures",
            severity="critical",
            summary=f"HITL reply delivery failed: {exc}",
        )
        incident_repository.append_event(
            incident_id=incident.id,
            event_type="hitl_delivery_failed",
            details=f"ticket_id={ticket_id};reason=provider_error",
        )
        raise HTTPException(status_code=502, detail="hitl_delivery_failed") from exc

    resolved = hitl_ticket_repository.resolve(ticket_id=ticket_id)
    return {
        "ticket_id": ticket_id,
        "delivered": True,
        "chat_id": ticket.target_chat_id,
        "message_id": message_id,
        "resolved": True,
        "status": resolved.status,
        "resolved_at": resolved.resolved_at,
    }


@app.post("/knowledge/extract")
def extract_knowledge_candidates(request: KnowledgeExtractRequest) -> dict[str, object]:
    try:
        extract_result = knowledge_candidate_repository.extract_from_transcripts(
            conversation_id=request.conversation_id
        )
    except Exception as exc:
        incident = incident_repository.ingest(
            fingerprint="knowledge_extraction_failures",
            severity="critical",
            summary=f"Knowledge extraction failed: {exc}",
        )
        incident_repository.append_event(
            incident_id=incident.id,
            event_type="knowledge_extract_failed",
            details=f"conversation_id={request.conversation_id}",
        )
        raise HTTPException(status_code=500, detail="knowledge_extraction_failed") from exc

    moderation_ids: list[int] = []
    for extracted in extract_result.new_candidates:
        moderation_row = knowledge_moderation_repository.create_pending(
            text=extracted.candidate_text,
            source_extraction_candidate_id=extracted.id,
        )
        moderation_ids.append(moderation_row.id)

    candidates = knowledge_candidate_repository.list_candidates(
        conversation_id=request.conversation_id
    )
    return {
        "inserted_candidates": extract_result.inserted,
        "enqueued_for_moderation": len(moderation_ids),
        "moderation_queue_ids": moderation_ids,
        "items": [
            {
                "id": item.id,
                "conversation_id": item.conversation_id,
                "source_message_id": item.source_message_id,
                "candidate_text": item.candidate_text,
            }
            for item in candidates
        ],
    }


@app.post("/knowledge/candidates")
def create_knowledge_candidate(request: KnowledgeCandidateCreateRequest) -> dict[str, object]:
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="empty_candidate_text")
    row = knowledge_moderation_repository.create_pending(text=request.text)
    return {
        "id": row.id,
        "status": row.status,
        "candidate_text": row.candidate_text,
        "source_extraction_candidate_id": row.source_extraction_candidate_id,
    }


@app.get("/knowledge/candidates")
def list_knowledge_candidates(status: str | None = None) -> dict[str, object]:
    rows = knowledge_moderation_repository.list_by_status(status)
    return {
        "items": [
            {
                "id": row.id,
                "candidate_text": row.candidate_text,
                "published_text": row.published_text,
                "status": row.status,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "source_extraction_candidate_id": row.source_extraction_candidate_id,
            }
            for row in rows
        ]
    }


@app.post("/knowledge/candidates/{candidate_id}/approve")
def approve_knowledge_candidate(
    candidate_id: int, request: KnowledgeCandidateApproveRequest
) -> dict[str, object]:
    try:
        publish_text = knowledge_moderation_repository.prepare_publish_text(
            candidate_id=candidate_id,
            edited_text=request.edited_text,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="candidate_not_found") from exc
    except ValueError as exc:
        if str(exc) == "invalid_status":
            raise HTTPException(status_code=409, detail="candidate_not_pending") from exc
        if str(exc) == "empty_publish_text":
            raise HTTPException(status_code=400, detail="empty_publish_text") from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    source_id = f"knowledge_candidate:{candidate_id}"
    try:
        inserted_chunks = rag_repository.ingest(source_id=source_id, text=publish_text)
    except Exception as exc:
        incident = incident_repository.ingest(
            fingerprint="knowledge_reindex_failures",
            severity="critical",
            summary=f"Knowledge moderation reindex failed: {exc}",
        )
        incident_repository.append_event(
            incident_id=incident.id,
            event_type="knowledge_reindex_failed",
            details=f"candidate_id={candidate_id};source_id={source_id}",
        )
        raise HTTPException(status_code=500, detail="knowledge_reindex_failed") from exc

    try:
        knowledge_moderation_repository.mark_approved(
            candidate_id=candidate_id,
            published_text=publish_text,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="candidate_not_found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="candidate_not_pending") from exc

    return {
        "id": candidate_id,
        "status": "approved",
        "published_text": publish_text,
        "source_id": source_id,
        "inserted_chunks": inserted_chunks,
    }


@app.post("/knowledge/candidates/{candidate_id}/reject")
def reject_knowledge_candidate(candidate_id: int) -> dict[str, object]:
    try:
        knowledge_moderation_repository.reject(candidate_id=candidate_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="candidate_not_found") from exc
    except ValueError as exc:
        if str(exc) == "invalid_status":
            raise HTTPException(status_code=409, detail="candidate_not_pending") from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": candidate_id, "status": "rejected"}


_OPERATOR_UPLOAD_TYPES = frozenset(
    {
        "pdf",
        "docx",
        "pptx",
        "txt",
        "image",
        "audio",
        "video",
        "inline_text",
        "xlsx",
        "csv",
        "html",
        "md",
        "rtf",
        "epub",
        "zip",
    }
)
_OPERATOR_UPLOAD_MEDIA_TYPES = frozenset({"audio", "video"})
_operator_transcriber: object | None = None


def _get_operator_transcriber() -> object:
    global _operator_transcriber
    if _operator_transcriber is None:
        from services.api.app.operator_uploads.extractors import WhisperTranscriber

        _operator_transcriber = WhisperTranscriber()
    return _operator_transcriber


async def _perform_operator_upload(request: OperatorUploadRequest) -> dict[str, object]:
    from services.api.app.operator_uploads.extractors import (
        EXTRACTORS,
        ExtractionError,
        binary_sha256,
        extract_media,
        soft_wrap,
    )

    logger.info(
        "operator_upload_received",
        extra={
            "operator_username": request.operator_username,
            "operator_short_id": request.operator_short_id,
            "source_file_type": request.source_file_type,
            "source_file_name": request.source_file_name,
            "is_confidential": request.is_confidential,
            "stored_binary_path": request.stored_binary_path,
            "project_id_requested": request.project_id,
            "project_slug_requested": request.project_slug,
        },
    )

    if request.source_file_type not in _OPERATOR_UPLOAD_TYPES:
        logger.info(
            "operator_upload_failed",
            extra={
                "operator_username": request.operator_username,
                "stage": "type_validation",
                "error": "unsupported_source_file_type",
                "source_file_type": request.source_file_type,
            },
        )
        raise HTTPException(status_code=422, detail="unsupported_source_file_type")

    sha: str | None = None
    if request.source_file_type == "inline_text":
        if not request.inline_text or not request.inline_text.strip():
            raise HTTPException(status_code=422, detail="empty_inline_text")
    else:
        if not request.stored_binary_path:
            raise HTTPException(status_code=422, detail="missing_stored_binary_path")
        binary_path = Path(request.stored_binary_path)
        if not binary_path.exists():
            raise HTTPException(status_code=404, detail="binary_not_found")
        sha = binary_sha256(binary_path)
        existing = knowledge_moderation_repository.find_by_binary_sha256(sha)
        if existing is not None:
            return {
                "candidate_id": existing.id,
                "source_id": f"knowledge_candidate:{existing.id}",
                "inserted_chunks": 0,
                "extracted_chars": 0,
                "is_confidential": existing.is_confidential,
                "deduplicated": True,
                "project_id": existing.project_id,
            }

    try:
        if request.source_file_type == "inline_text":
            raw_text = request.inline_text.strip()  # type: ignore[union-attr]
        elif request.source_file_type in _OPERATOR_UPLOAD_MEDIA_TYPES:
            raw_text = await extract_media(
                request.source_file_type,
                Path(request.stored_binary_path),  # type: ignore[arg-type]
                transcriber=_get_operator_transcriber(),  # type: ignore[arg-type]
            )
        else:
            extractor = EXTRACTORS[request.source_file_type]
            raw_text = extractor(Path(request.stored_binary_path))  # type: ignore[arg-type]
            if not raw_text or not raw_text.strip():
                raise ExtractionError("empty_text")
    except ExtractionError as exc:
        raise HTTPException(status_code=422, detail=exc.reason) from exc
    except HTTPException:
        raise
    except Exception as exc:
        incident = incident_repository.ingest(
            fingerprint="operator_upload_failures",
            severity="critical",
            summary=f"Operator upload extraction failed: {exc}",
        )
        incident_repository.append_event(
            incident_id=incident.id,
            event_type="operator_upload_failed",
            details=(
                f"operator={request.operator_username};"
                f"type={request.source_file_type};"
                f"path={request.stored_binary_path}"
            ),
        )
        logger.info(
            "operator_upload_failed",
            extra={
                "operator_username": request.operator_username,
                "stage": "extraction",
                "error": repr(exc),
                "source_file_type": request.source_file_type,
                "incident_id": incident.id,
            },
        )
        raise HTTPException(status_code=500, detail="operator_upload_failed") from exc

    logger.info(
        "operator_upload_extracted",
        extra={
            "operator_username": request.operator_username,
            "operator_short_id": request.operator_short_id,
            "source_file_type": request.source_file_type,
            "extracted_text_length": len(raw_text),
        },
    )

    wrapped = soft_wrap(raw_text)
    if not wrapped.strip():
        logger.info(
            "operator_upload_failed",
            extra={
                "operator_username": request.operator_username,
                "stage": "soft_wrap",
                "error": "empty_text_after_wrap",
                "raw_text_length": len(raw_text),
            },
        )
        raise HTTPException(status_code=422, detail="empty_text")

    resolved_project_id = _resolve_upload_project_id(
        operator_username=request.operator_username,
        project_id=request.project_id,
        project_slug=request.project_slug,
        short_id=request.operator_short_id,
    )
    candidate = knowledge_moderation_repository.create_approved_operator_upload(
        candidate_text=raw_text,
        published_text=wrapped,
        operator_username=request.operator_username,
        is_confidential=request.is_confidential,
        source_file_name=request.source_file_name,
        source_file_type=request.source_file_type,
        stored_binary_path=request.stored_binary_path,
        binary_sha256=sha,
        operator_short_id=request.operator_short_id,
    )
    if resolved_project_id is not None:
        knowledge_moderation_repository.set_project_id(
            candidate_id=candidate.id, project_id=resolved_project_id
        )
    source_id = f"knowledge_candidate:{candidate.id}"
    inserted_chunks = rag_repository.ingest(
        source_id=source_id,
        text=wrapped,
        is_confidential=request.is_confidential,
        project_id=resolved_project_id,
    )
    logger.info(
        "operator_upload_ingested",
        extra={
            "operator_username": request.operator_username,
            "operator_short_id": request.operator_short_id,
            "candidate_id": candidate.id,
            "source_id": source_id,
            "inserted_chunks": inserted_chunks,
            "project_id": resolved_project_id,
            "is_confidential": request.is_confidential,
            "extracted_chars": len(wrapped),
        },
    )
    return {
        "candidate_id": candidate.id,
        "source_id": source_id,
        "inserted_chunks": inserted_chunks,
        "extracted_chars": len(wrapped),
        "is_confidential": request.is_confidential,
        "deduplicated": False,
        "project_id": resolved_project_id,
    }


@app.post("/knowledge/operator_upload")
async def operator_upload(request: OperatorUploadRequest) -> dict[str, object]:
    return await _perform_operator_upload(request)


_EXTENSION_TO_SOURCE_TYPE: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".txt": "txt",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".bmp": "image",
    ".webp": "image",
    ".tiff": "image",
    ".mp3": "audio",
    ".wav": "audio",
    ".ogg": "audio",
    ".oga": "audio",
    ".m4a": "audio",
    ".flac": "audio",
    ".mp4": "video",
    ".mov": "video",
    ".mkv": "video",
    ".webm": "video",
    ".avi": "video",
    ".xlsx": "xlsx",
    ".csv": "csv",
    ".html": "html",
    ".htm": "html",
    ".md": "md",
    ".markdown": "md",
    ".rtf": "rtf",
    ".epub": "epub",
    ".zip": "zip",
}


def _infer_source_file_type(filename: str | None) -> str | None:
    if not filename:
        return None
    suffix = Path(filename).suffix.lower()
    return _EXTENSION_TO_SOURCE_TYPE.get(suffix)


@app.post("/knowledge/operator_upload_multipart")
async def operator_upload_multipart(
    operator_username: Annotated[str, Form()],
    is_confidential: Annotated[bool, Form()] = False,
    source_file_type: Annotated[str | None, Form()] = None,
    inline_text: Annotated[str | None, Form()] = None,
    upload: Annotated[UploadFile | None, File()] = None,
) -> dict[str, object]:
    has_file = upload is not None
    has_inline = inline_text is not None and inline_text.strip() != ""
    if has_file and has_inline:
        raise HTTPException(status_code=422, detail="file_and_inline_text_both_set")
    if not has_file and not has_inline:
        raise HTTPException(status_code=422, detail="file_or_inline_text_required")

    if has_inline:
        request = OperatorUploadRequest(
            operator_username=operator_username,
            source_file_type="inline_text",
            inline_text=inline_text,
            is_confidential=is_confidential,
        )
        return await _perform_operator_upload(request)

    assert upload is not None
    inferred_type = source_file_type or _infer_source_file_type(upload.filename)
    if inferred_type is None:
        raise HTTPException(status_code=422, detail="unknown_source_file_type")

    storage_dir = Path(settings.operator_upload_storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.filename or "").suffix
    stored_path = storage_dir / f"{uuid.uuid4().hex}{suffix}"
    contents = await upload.read()
    if len(contents) > settings.operator_upload_max_bytes:
        raise HTTPException(status_code=413, detail="upload_too_large")
    stored_path.write_bytes(contents)

    request = OperatorUploadRequest(
        operator_username=operator_username,
        source_file_type=inferred_type,
        source_file_name=upload.filename,
        stored_binary_path=str(stored_path),
        is_confidential=is_confidential,
    )
    return await _perform_operator_upload(request)


def _serialize_nl_session(session: object) -> dict[str, object]:
    return {
        "id": session.id,
        "tenant_id": session.tenant_id,
        "user_id": session.user_id,
        "utterance": session.utterance,
        "intent": session.intent,
        "draft_text": session.draft_text,
        "status": session.status,
        "confirm_token": session.confirm_token,
        "knowledge_version_id": session.knowledge_version_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _serialize_nl_version(version: object) -> dict[str, object]:
    return {
        "id": version.id,
        "tenant_id": version.tenant_id,
        "version_number": version.version_number,
        "source_text": version.source_text,
        "status": version.status,
        "nl_session_id": version.nl_session_id,
        "source_id": version.source_id,
        "created_at": version.created_at,
    }


def _check_nl_ops_enabled() -> None:
    if not settings.nl_ops_enabled:
        raise HTTPException(status_code=503, detail="nl_ops_disabled")


def _check_nl_ops_admin(user_id: str) -> None:
    allow = settings.nl_ops_admin_user_id_list()
    if allow and user_id not in allow:
        raise HTTPException(status_code=403, detail="nl_ops_user_not_authorized")


@app.post("/knowledge/nl-ops")
def propose_nl_op(request: NlOpProposeRequest) -> dict[str, object]:
    _check_nl_ops_enabled()
    _check_nl_ops_admin(request.user_id)
    tenant_id = request.tenant_id or settings.nl_ops_default_tenant_id
    try:
        session = nl_knowledge_ops_repository.propose(
            tenant_id=tenant_id,
            user_id=request.user_id,
            utterance=request.utterance,
        )
    except NlKnowledgeOpsError as exc:
        message = str(exc)
        if message in {"tenant_id_required", "user_id_required", "utterance_required"}:
            raise HTTPException(status_code=400, detail=message) from exc
        incident = incident_repository.ingest(  # pragma: no cover - defensive guard
            fingerprint="nl_ops_propose_failures",
            severity="critical",
            summary=f"NL ops propose failed: {message}",
        )
        incident_repository.append_event(  # pragma: no cover
            incident_id=incident.id,
            event_type="nl_ops_propose_failed",
            details=message,
        )
        raise HTTPException(  # pragma: no cover
            status_code=500, detail="nl_ops_propose_failed"
        ) from exc
    return _serialize_nl_session(session)


@app.post("/knowledge/nl-ops/{session_id}/confirm")
def confirm_nl_op(session_id: int, request: NlOpConfirmRequest) -> dict[str, object]:
    _check_nl_ops_enabled()
    try:
        session, version = nl_knowledge_ops_repository.confirm(
            session_id=session_id,
            confirm_token=request.confirm_token,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="nl_op_session_not_found") from exc
    except NlKnowledgeOpsError as exc:
        message = str(exc)
        if message == "invalid_confirm_token":
            raise HTTPException(status_code=400, detail=message) from exc
        if message.startswith("invalid_status") or message == "already_confirmed":
            raise HTTPException(status_code=409, detail=message) from exc
        raise HTTPException(  # pragma: no cover - defensive guard
            status_code=400, detail=message
        ) from exc

    if session.intent != "deprecate":
        try:
            rag_repository.ingest(source_id=version.source_id, text=version.source_text)
        except Exception as exc:
            incident = incident_repository.ingest(
                fingerprint="nl_knowledge_reindex_failures",
                severity="critical",
                summary=f"NL ops reindex failed: {exc}",
            )
            incident_repository.append_event(
                incident_id=incident.id,
                event_type="nl_knowledge_reindex_failed",
                details=f"version_id={version.id};source_id={version.source_id}",
            )
            raise HTTPException(status_code=500, detail="nl_knowledge_reindex_failed") from exc
    return {
        "session": _serialize_nl_session(session),
        "version": _serialize_nl_version(version),
    }


@app.post("/knowledge/nl-ops/{session_id}/cancel")
def cancel_nl_op(session_id: int) -> dict[str, object]:
    _check_nl_ops_enabled()
    try:
        session = nl_knowledge_ops_repository.cancel(session_id=session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="nl_op_session_not_found") from exc
    except NlKnowledgeOpsError as exc:
        message = str(exc)
        if message.startswith("invalid_status"):
            raise HTTPException(status_code=409, detail=message) from exc
        raise HTTPException(  # pragma: no cover - defensive guard
            status_code=400, detail=message
        ) from exc
    return _serialize_nl_session(session)


@app.get("/knowledge/nl-ops/{session_id}")
def get_nl_op(session_id: int) -> dict[str, object]:
    try:
        session = nl_knowledge_ops_repository.get_session(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="nl_op_session_not_found") from exc
    return _serialize_nl_session(session)


@app.get("/knowledge/nl-ops")
def list_nl_ops(tenant_id: str | None = None) -> dict[str, object]:
    sessions = nl_knowledge_ops_repository.list_sessions(tenant_id=tenant_id)
    return {"items": [_serialize_nl_session(item) for item in sessions]}


@app.get("/knowledge/versions")
def list_knowledge_versions(tenant_id: str | None = None) -> dict[str, object]:
    versions = nl_knowledge_ops_repository.list_versions(tenant_id=tenant_id)
    return {"items": [_serialize_nl_version(version) for version in versions]}


@app.get("/knowledge/nl-ops-audit")
def list_nl_ops_audit(tenant_id: str | None = None) -> dict[str, object]:
    logs = nl_knowledge_ops_repository.list_audit_logs(tenant_id=tenant_id)
    return {
        "items": [
            {
                "id": log.id,
                "tenant_id": log.tenant_id,
                "user_id": log.user_id,
                "session_id": log.session_id,
                "op_type": log.op_type,
                "details": log.details,
                "created_at": log.created_at,
            }
            for log in logs
        ]
    }


def _serialize_correction(correction: object) -> dict[str, object]:
    return {
        "id": correction.id,
        "trace_id": correction.trace_id,
        "tenant_id": correction.tenant_id,
        "user_id": correction.user_id,
        "branch": correction.branch,
        "status": correction.status,
        "draft_text": correction.draft_text,
        "source_id": correction.source_id,
        "candidate_id": correction.candidate_id,
        "created_at": correction.created_at,
        "updated_at": correction.updated_at,
    }


def _ensure_trace_exists(trace_id: str) -> None:
    try:
        answer_trace_repository.get_by_trace_id(trace_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="answer_trace_not_found") from exc


@app.post("/answer-traces/{trace_id}/open")
def record_trace_open(trace_id: str, request: TraceOpenRequest) -> dict[str, object]:
    _ensure_trace_exists(trace_id)
    try:
        trace_correction_repository.record_open(
            trace_id=trace_id,
            tenant_id=request.tenant_id,
            user_id=request.user_id,
        )
    except TraceCorrectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"trace_id": trace_id, "status": "logged"}


@app.post("/answer-traces/{trace_id}/corrections")
def submit_trace_correction(
    trace_id: str, request: TraceCorrectionRequest
) -> dict[str, object]:
    _ensure_trace_exists(trace_id)
    if request.branch not in {BRANCH_PUBLISH, BRANCH_MODERATION}:
        raise HTTPException(status_code=400, detail="invalid_branch")
    if request.branch == BRANCH_PUBLISH:
        try:
            correction = trace_correction_repository.submit_publish(
                trace_id=trace_id,
                tenant_id=request.tenant_id,
                user_id=request.user_id,
                edited_text=request.edited_text,
            )
        except TraceCorrectionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            rag_repository.ingest(
                source_id=correction.source_id,
                text=correction.draft_text,
            )
        except Exception as exc:
            incident = incident_repository.ingest(
                fingerprint="trace_correction_reindex_failures",
                severity="critical",
                summary=f"Trace correction reindex failed: {exc}",
            )
            incident_repository.append_event(
                incident_id=incident.id,
                event_type="trace_correction_reindex_failed",
                details=f"correction_id={correction.id};trace_id={trace_id}",
            )
            raise HTTPException(
                status_code=500, detail="trace_correction_reindex_failed"
            ) from exc
        return _serialize_correction(correction)

    candidate = knowledge_moderation_repository.create_pending(text=request.edited_text)
    try:
        correction = trace_correction_repository.submit_moderation(
            trace_id=trace_id,
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            edited_text=request.edited_text,
            candidate_id=candidate.id,
        )
    except TraceCorrectionError as exc:  # pragma: no cover - defensive guard
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_correction(correction)


@app.get("/answer-traces/{trace_id}/corrections")
def list_trace_corrections(trace_id: str) -> dict[str, object]:
    _ensure_trace_exists(trace_id)
    return {
        "items": [
            _serialize_correction(correction)
            for correction in trace_correction_repository.list_for_trace(trace_id)
        ],
    }


@app.get("/answer-traces/{trace_id}/audit")
def list_trace_audit(trace_id: str) -> dict[str, object]:
    _ensure_trace_exists(trace_id)
    return {"items": trace_correction_repository.list_audit(trace_id=trace_id)}


def _serialize_trace(trace: object) -> dict[str, object]:
    return {
        "trace_id": trace.trace_id,
        "created_at": trace.created_at,
        "request_text": trace.request_text,
        "model_id": trace.model_id,
        "model_provider": trace.model_provider,
        "latency_ms": trace.latency_ms,
        "response_mode": trace.response_mode,
        "guardrails_applied": trace.guardrails_applied,
        "guardrail_outcome": trace.guardrail_outcome,
        "guardrail_reasons": trace.guardrail_reasons,
        "guardrail_score": trace.guardrail_score,
        "grounded": trace.grounded,
        "no_retrieval_hit": trace.no_retrieval_hit,
        "confidence": trace.confidence,
        "retrieval": trace.retrieval,
        "limitations": trace.limitations,
    }


@app.get("/answer-traces")
def list_answer_traces(limit: int = 50) -> dict[str, object]:
    return {
        "items": [_serialize_trace(trace) for trace in answer_trace_repository.list_traces(
            limit=limit
        )]
    }


@app.get("/answer-traces/{trace_id}")
def get_answer_trace(trace_id: str) -> dict[str, object]:
    try:
        trace = answer_trace_repository.get_by_trace_id(trace_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="answer_trace_not_found") from exc
    return _serialize_trace(trace)


def _serialize_backup(backup: object) -> dict[str, object]:
    return {
        "id": backup.id,
        "started_at": backup.started_at,
        "completed_at": backup.completed_at,
        "status": backup.status,
        "archive_path": backup.archive_path,
        "size_bytes": backup.size_bytes,
        "source_paths": backup.source_paths,
        "included_paths": backup.included_paths,
        "error_message": backup.error_message,
    }


@app.post("/backups/run")
def run_backup() -> dict[str, object]:
    try:
        backup = backup_repository.run_backup()
    except BackupError as exc:
        incident = incident_repository.ingest(
            fingerprint="backup_failures",
            severity="critical",
            summary=f"Backup run failed: {exc}",
        )
        incident_repository.append_event(
            incident_id=incident.id,
            event_type="backup_failed",
            details=str(exc),
        )
        raise HTTPException(status_code=500, detail="backup_failed") from exc
    return _serialize_backup(backup)


@app.get("/backups")
def list_backups() -> dict[str, object]:
    return {"items": [_serialize_backup(backup) for backup in backup_repository.list_backups()]}


@app.get("/backups/last-successful")
def get_last_successful_backup() -> dict[str, object]:
    backup = backup_repository.latest_successful()
    if backup is None:
        return {"backup": None}
    return {"backup": _serialize_backup(backup)}


@app.get("/backups/{backup_id}")
def get_backup(backup_id: int) -> dict[str, object]:
    try:
        backup = backup_repository.get(backup_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="backup_not_found") from exc
    return _serialize_backup(backup)


@app.post("/backups/{backup_id}/restore")
def restore_backup(backup_id: int, request: BackupRestoreRequest) -> dict[str, object]:
    try:
        result = backup_repository.restore(
            backup_id=backup_id,
            confirm_token=request.confirm_token,
            target_root=request.target_root,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="backup_not_found") from exc
    except BackupError as exc:
        message = str(exc)
        if message == "invalid_confirm_token":
            raise HTTPException(status_code=400, detail="invalid_confirm_token") from exc
        incident = incident_repository.ingest(
            fingerprint="backup_restore_failures",
            severity="critical",
            summary=f"Backup restore failed: {message}",
        )
        incident_repository.append_event(
            incident_id=incident.id,
            event_type="backup_restore_failed",
            details=f"backup_id={backup_id};error={message}",
        )
        raise HTTPException(status_code=500, detail="backup_restore_failed") from exc
    return {
        "backup_id": result.backup_id,
        "restored_paths": result.restored_paths,
    }


# ---------------------------------------------------------------------------
# Epic 11 / story 11.02 — Google Calendar OAuth connect flow
# ---------------------------------------------------------------------------

_CALENDAR_OAUTH_RATE_LIMIT = 10
_CALENDAR_OAUTH_RATE_WINDOW_SECONDS = 60.0

# FR-18: on successful OAuth consent the api DMs the operator a Russian
# confirmation (in addition to the HTML success page in the browser). The
# copy nudges the operator toward catalog-only entries via /service add.
_CALENDAR_CONNECTED_DM = (
    "✅ Календарь подключён. "
    "Услуги, у которых указано расписание, "
    "теперь будут проверяться по вашему календарю. "
    "Чтобы добавить услугу: `/service add <название>` "
    "или просто напишите «добавь услугу …»."
)
# In-memory coarse throttle: {bucket_key: [monotonic timestamps]}. Bounds the
# abuse surface of the unauthenticated callback (each hit triggers a token
# exchange) and the internal initiate. Per-state single-use bounds replay; this
# bounds volume.
_calendar_oauth_hits: dict[str, list[float]] = {}


def _calendar_oauth_rate_limited(bucket: str) -> bool:
    now = time.monotonic()
    window_start = now - _CALENDAR_OAUTH_RATE_WINDOW_SECONDS
    hits = [ts for ts in _calendar_oauth_hits.get(bucket, []) if ts > window_start]
    if len(hits) >= _CALENDAR_OAUTH_RATE_LIMIT:
        _calendar_oauth_hits[bucket] = hits
        return True
    hits.append(now)
    _calendar_oauth_hits[bucket] = hits
    return False


def _calendar_callback_html(*, ok: bool) -> str:
    if ok:
        title = "Календарь подключён"
        body = "Доступ к календарю предоставлен. Можете вернуться в Telegram."
    else:
        title = "Не удалось подключить календарь"
        body = (
            "Ссылка устарела или произошла ошибка. "
            "Запросите новую ссылку в Telegram и попробуйте снова."
        )
    return (
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        f"<title>{title}</title></head><body><h1>{title}</h1>"
        f"<p>{body}</p></body></html>"
    )


class CalendarConnectRequest(BaseModel):
    project_id: int
    operator: str


@app.post("/calendar/connect/initiate")
async def calendar_connect_initiate(
    request: CalendarConnectRequest,
    _principal: Annotated[str, Depends(require_internal_token)],
    http_request: Request,
) -> dict[str, object]:
    if calendar_oauth_client is None:
        raise HTTPException(status_code=503, detail="calendar_oauth_not_configured")
    client_host = http_request.client.host if http_request.client else "unknown"
    if _calendar_oauth_rate_limited(f"initiate:{client_host}"):
        raise HTTPException(status_code=429, detail="rate_limited")
    # Note: no project-enabled / designated-operator precondition here. The
    # OAuth callback is the authoritative gate: on success it auto-enables the
    # project and records the requesting operator as the designated calendar
    # operator (PR #76). Requiring enabled=1 here would make bootstrap
    # impossible, and requiring an existing designated operator would block
    # operator handover / re-consent flows.
    state = await asyncio.to_thread(
        calendar_oauth_state_repository.create,
        project_id=request.project_id,
        operator=request.operator,
        ttl_seconds=settings.calendar_oauth_state_ttl_seconds,
        now=datetime.now(UTC),
    )
    consent_url = calendar_oauth_client.build_consent_url(state=state)
    logger.info(
        "calendar_oauth_initiated",
        extra={"project_id": request.project_id, "operator": request.operator},
    )
    return {"consent_url": consent_url}


@app.get("/calendar/oauth/callback", response_class=HTMLResponse)
async def calendar_oauth_callback(
    http_request: Request,
    state: str | None = None,
    code: str | None = None,
) -> HTMLResponse:
    if calendar_oauth_client is None or calendar_token_repository is None:
        return HTMLResponse(_calendar_callback_html(ok=False), status_code=503)
    client_host = http_request.client.host if http_request.client else "unknown"
    if _calendar_oauth_rate_limited(f"callback:{client_host}"):
        return HTMLResponse(_calendar_callback_html(ok=False), status_code=429)
    if not state or not code:
        return HTMLResponse(_calendar_callback_html(ok=False), status_code=400)
    try:
        pending = await asyncio.to_thread(
            calendar_oauth_state_repository.consume, state, now=datetime.now(UTC)
        )
    except InvalidOAuthState:
        logger.warning("calendar_oauth_callback_invalid_state")
        return HTMLResponse(_calendar_callback_html(ok=False), status_code=400)
    try:
        tokens = await asyncio.to_thread(calendar_oauth_client.exchange_code, code=code)
    except OAuthExchangeError:
        logger.warning(
            "calendar_oauth_callback_exchange_failed",
            extra={"project_id": pending.project_id, "operator": pending.operator},
        )
        return HTMLResponse(_calendar_callback_html(ok=False), status_code=400)
    # FR-18 DM guard (post-R2 bugfix): the "✅ Календарь подключён" DM should
    # only fire on a *fresh* connect, not on every successful callback.
    # Re-clicking the consent URL during dev, or any other repeat consent for
    # an already-connected (project, operator), used to spam the operator
    # with the connection-confirmation message. Capture token presence BEFORE
    # the upsert; we only DM when there was no prior token (first-time connect
    # or post-disconnect reconnect, both of which are legitimately "just got
    # connected" moments from the operator's point of view).
    token_existed_before_upsert = False
    try:
        await asyncio.to_thread(
            calendar_token_repository.get_refresh_token,
            pending.project_id,
            pending.operator,
        )
        token_existed_before_upsert = True
    except TokenNotFound:
        token_existed_before_upsert = False
    await asyncio.to_thread(
        calendar_token_repository.upsert,
        pending.project_id,
        pending.operator,
        tokens.refresh_token,
    )
    # /connect_calendar IS the enable action: a successful consent +
    # token upsert atomically flips the project to enabled and records
    # the connecting operator as the designated calendar operator. If the
    # project is already enabled, we preserve the existing
    # project_timezone / lookahead_days and only update the designated
    # operator. There is no separate /calendar_on path — re-enable after
    # /calendar_off means the operator re-runs /connect_calendar.
    try:
        existing = await asyncio.to_thread(
            calendar_settings_repository.get, pending.project_id
        )
        enable_kwargs: dict[str, object] = {
            "calendar_operator": pending.operator,
        }
        if existing is not None:
            enable_kwargs["project_timezone"] = existing.project_timezone
            enable_kwargs["lookahead_days"] = existing.lookahead_days
        await asyncio.to_thread(
            calendar_settings_repository.enable,
            pending.project_id,
            **enable_kwargs,
        )
    except Exception:
        # Token upsert succeeded but enablement did not — surface the
        # failure rather than silently leave a half-state. The operator
        # can re-run /connect_calendar to retry; do not render a
        # misleading success page.
        logger.exception(
            "calendar_oauth_callback_enable_failed",
            extra={
                "project_id": pending.project_id,
                "operator": pending.operator,
            },
        )
        return HTMLResponse(_calendar_callback_html(ok=False), status_code=500)
    logger.info(
        "calendar_oauth_connected",
        extra={"project_id": pending.project_id, "operator": pending.operator},
    )
    # FR-18: Russian Telegram DM to the operator confirming the connection
    # (in addition to the HTML success page). DM failures MUST NOT corrupt
    # the OAuth success signal to Google or to the browser — log + swallow.
    # Fresh-connect-only guard: skip the DM when the operator was already
    # connected to this project (re-consent / token refresh on the same
    # registration). Without this guard, every successful callback for an
    # already-connected operator spams the "Календарь подключён" message —
    # which the operator complained about during dev (the consent URL gets
    # re-clicked across api restarts and each click DMs again).
    if token_existed_before_upsert:
        logger.info(
            "calendar_connect_dm_skipped_already_connected",
            extra={
                "project_id": pending.project_id,
                "operator": pending.operator,
            },
        )
        return HTMLResponse(_calendar_callback_html(ok=True), status_code=200)
    operator_chat_id: int | None = None
    try:
        record = await asyncio.to_thread(
            operator_repository.find_by_username, pending.operator
        )
    except Exception:
        record = None
    if record is not None and record.chat_id is not None:
        operator_chat_id = record.chat_id
    else:
        fallback = settings.hitl_primary_operator_chat_id
        if fallback:
            try:
                operator_chat_id = int(fallback)
            except (TypeError, ValueError):
                operator_chat_id = None
    if operator_chat_id is None:
        logger.info(
            "calendar_connect_dm_no_chat_id",
            extra={
                "trace_id": None,
                "project_id": pending.project_id,
                "operator": pending.operator,
            },
        )
    else:
        try:
            await telegram_bot_sender.send_message(
                chat_id=operator_chat_id, text=_CALENDAR_CONNECTED_DM
            )
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.warning(
                "calendar_connect_dm_failed",
                extra={
                    "trace_id": None,
                    "project_id": pending.project_id,
                    "operator": pending.operator,
                    "error_repr": repr(exc),
                },
            )
        except Exception as exc:  # defensive: never break the OAuth callback
            logger.warning(
                "calendar_connect_dm_failed",
                extra={
                    "trace_id": None,
                    "project_id": pending.project_id,
                    "operator": pending.operator,
                    "error_repr": repr(exc),
                },
            )
    return HTMLResponse(_calendar_callback_html(ok=True), status_code=200)


class CalendarDisconnectRequest(BaseModel):
    project_id: int
    operator: str
    actor_role: str = "operator"


@app.post("/calendar/disconnect")
async def calendar_disconnect(
    request: CalendarDisconnectRequest,
    _principal: Annotated[str, Depends(require_internal_token)],
) -> dict[str, object]:
    # Operator-only path (FR-18/FR-21): an admin attempting to disconnect/delete
    # is rejected. Disable keeps the token; only the operator removes it.
    authorize_calendar_disconnect(actor_role=request.actor_role)
    if calendar_oauth_client is None or calendar_token_repository is None:
        raise HTTPException(status_code=503, detail="calendar_oauth_not_configured")
    try:
        refresh_token = await asyncio.to_thread(
            calendar_token_repository.get_refresh_token,
            request.project_id,
            request.operator,
        )
    except TokenNotFound:
        refresh_token = None
    if refresh_token is not None:
        await calendar_oauth_client.revoke(refresh_token=refresh_token)
    await asyncio.to_thread(
        calendar_token_repository.delete, request.project_id, request.operator
    )
    logger.info(
        "calendar_oauth_disconnected",
        extra={"project_id": request.project_id, "operator": request.operator},
    )
    return {"disconnected": True}


# --- Calendar disable + service config (story 11.08) -----------------------
#
# Enable is implicit in /connect_calendar (the OAuth callback flips
# ``enabled=1`` and records the connecting operator atomically with the token
# upsert) — there is no separate enable endpoint. Disable + service-rule
# config remain explicit and are usable by both the project's designated
# calendar operator AND an admin (FR-21). Disable keeps the stored token; only
# the operator may disconnect/delete (admin → 403). The operator-vs-admin
# rule is enforced centrally via ``services.api.app.calendar.authorization``.


class CalendarDisableRequest(BaseModel):
    actor: str
    actor_role: str = "operator"


class CalendarServiceRuleRequest(BaseModel):
    actor: str
    actor_role: str = "operator"
    rule_id: int | None = None
    name: str | None = None
    duration_minutes: int | None = None
    working_hours: dict | None = None
    service_days: list | None = None
    date_exceptions: list | None = None


class CalendarServiceDeleteRequest(BaseModel):
    actor: str
    actor_role: str = "operator"


def _service_rule_to_dict(rule: ServiceRule) -> dict[str, object]:
    return {
        "id": rule.id,
        "project_id": rule.project_id,
        "name": rule.name,
        "duration_minutes": rule.duration_minutes,
        "working_hours": rule.working_hours,
        "service_days": rule.service_days,
        "date_exceptions": rule.date_exceptions,
        "updated_at": rule.updated_at,
    }


@app.post("/calendar/projects/{project_id}/disable")
async def calendar_project_disable(
    project_id: int,
    request: CalendarDisableRequest,
    _principal: Annotated[str, Depends(require_internal_token)],
) -> dict[str, object]:
    project_settings = await asyncio.to_thread(
        calendar_settings_repository.get, project_id
    )
    authorize_calendar_config(
        actor=request.actor,
        actor_role=request.actor_role,
        project_settings=project_settings,
    )
    # NB: disable flips enabled=0 only; the stored token is intentionally kept.
    await asyncio.to_thread(calendar_settings_repository.disable, project_id)
    logger.info(
        "calendar_project_disabled",
        extra={"project_id": project_id, "actor_role": request.actor_role},
    )
    return {"enabled": False}


@app.get("/calendar/projects/{project_id}/settings")
async def calendar_project_settings_view(
    project_id: int,
    _principal: Annotated[str, Depends(require_internal_token)],
) -> dict[str, object]:
    project_settings = await asyncio.to_thread(
        calendar_settings_repository.get, project_id
    )
    rules = await asyncio.to_thread(
        calendar_settings_repository.list_service_rules, project_id
    )
    if project_settings is None:
        enabled = False
        calendar_operator: str | None = None
        project_timezone = "Europe/Moscow"
        lookahead_days = 60
        updated_at: str | None = None
    else:
        enabled = project_settings.enabled
        calendar_operator = project_settings.calendar_operator
        project_timezone = project_settings.project_timezone
        lookahead_days = project_settings.lookahead_days
        updated_at = project_settings.updated_at
    return {
        "project_id": project_id,
        "enabled": enabled,
        "calendar_operator": calendar_operator,
        "project_timezone": project_timezone,
        "lookahead_days": lookahead_days,
        "updated_at": updated_at,
        "service_rules": [_service_rule_to_dict(rule) for rule in rules],
    }


@app.post("/calendar/projects/{project_id}/services")
async def calendar_project_service_upsert(
    project_id: int,
    request: CalendarServiceRuleRequest,
    _principal: Annotated[str, Depends(require_internal_token)],
) -> dict[str, object]:
    # Epic 13 (story 13.02): this endpoint is 60-day-deprecated; the canonical
    # surface lives at ``POST /api/projects/{id}/services``. The deprecation log
    # is the ONLY behavioral change — the rest of the handler keeps the Epic-11
    # contract (``rule_id``-keyed updates, ``{"id": ...}`` response shape) so
    # existing callers see no breakage. The slash-command alias hint DM is owned
    # by story 13.03; here we only emit the log.
    logger.info(
        "deprecation_warning_calendar_services_endpoint",
        extra={
            "endpoint": "POST /calendar/projects/{project_id}/services",
            "project_id": project_id,
            "actor_role": request.actor_role,
            "canonical_endpoint": "POST /api/projects/{project_id}/services",
        },
    )
    project_settings = await asyncio.to_thread(
        calendar_settings_repository.get, project_id
    )
    authorize_calendar_config(
        actor=request.actor,
        actor_role=request.actor_role,
        project_settings=project_settings,
    )
    try:
        validate_service_rule(
            duration_minutes=request.duration_minutes,
            working_hours=request.working_hours,
            service_days=request.service_days,
            date_exceptions=request.date_exceptions,
        )
    except ServiceRuleValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from exc
    rule_id = await asyncio.to_thread(
        _upsert_service_rule_call,
        project_id=project_id,
        request=request,
    )
    logger.info(
        "calendar_service_rule_upserted",
        extra={"project_id": project_id, "rule_id": rule_id},
    )
    return {"id": rule_id}


def _upsert_service_rule_call(
    *, project_id: int, request: CalendarServiceRuleRequest
) -> int:
    return calendar_settings_repository.upsert_service_rule(
        project_id=project_id,
        name=request.name,
        duration_minutes=request.duration_minutes,
        working_hours=request.working_hours,
        service_days=request.service_days,
        date_exceptions=request.date_exceptions,
        rule_id=request.rule_id,
    )


@app.delete("/calendar/projects/{project_id}/services/{rule_id}")
async def calendar_project_service_delete(
    project_id: int,
    rule_id: int,
    request: CalendarServiceDeleteRequest,
    _principal: Annotated[str, Depends(require_internal_token)],
) -> dict[str, object]:
    # Epic 13 (story 13.02): deprecated alias for
    # ``DELETE /api/projects/{project_id}/services/{service_id}``. The Epic-11
    # behavior (admin-allowed when designated operator) is preserved here on
    # purpose so existing automation keeps working — the canonical endpoint is
    # stricter (operator-only via ``authorize_service_remove``).
    logger.info(
        "deprecation_warning_calendar_services_endpoint",
        extra={
            "endpoint": "DELETE /calendar/projects/{project_id}/services/{rule_id}",
            "project_id": project_id,
            "actor_role": request.actor_role,
            "canonical_endpoint": "DELETE /api/projects/{project_id}/services/{service_id}",
        },
    )
    project_settings = await asyncio.to_thread(
        calendar_settings_repository.get, project_id
    )
    authorize_calendar_config(
        actor=request.actor,
        actor_role=request.actor_role,
        project_settings=project_settings,
    )
    await asyncio.to_thread(
        calendar_settings_repository.delete_service_rule, rule_id
    )
    logger.info(
        "calendar_service_rule_deleted",
        extra={"project_id": project_id, "rule_id": rule_id},
    )
    return {"deleted": True}


# --- Canonical /api/projects/{id}/services surface (Epic 13, story 13.02) ---
#
# The single structured catalog of operator-curated services per project. Add /
# edit is shared with admins (FR-21) via ``authorize_calendar_config``; remove
# is operator-only (FR-18/FR-21 destructive-op rule) via the new
# ``authorize_service_remove`` helper. Writes serialize through the per-row
# ``acquire_service_upsert_lock`` so concurrent same-name upserts cannot race
# (the unique-index expression catches inter-process collisions).


class ProjectServiceUpsertRequest(BaseModel):
    actor: str
    actor_role: str = "operator"
    name: str
    description: str | None = None
    price_text: str | None = None
    tags: list | None = None
    duration_minutes: int | None = None
    working_hours: dict | None = None
    service_days: list | None = None
    date_exceptions: list | None = None


class ProjectServiceDeleteRequest(BaseModel):
    actor: str
    actor_role: str = "operator"


def _project_service_to_dict(service: ProjectService) -> dict[str, object]:
    return {
        "id": service.id,
        "project_id": service.project_id,
        "name": service.name,
        "description": service.description,
        "price_text": service.price_text,
        "tags": service.tags,
        "duration_minutes": service.duration_minutes,
        "working_hours": service.working_hours,
        "service_days": service.service_days,
        "date_exceptions": service.date_exceptions,
        "updated_at": service.updated_at,
    }


@app.get("/api/projects/{project_id}/services")
async def api_project_services_list(
    project_id: int,
    _principal: Annotated[str, Depends(require_internal_token)],
) -> dict[str, object]:
    services = await asyncio.to_thread(
        project_services_repository.list_for_project, project_id=project_id
    )
    return {
        "project_id": project_id,
        "services": [_project_service_to_dict(s) for s in services],
    }


def _upsert_project_service_call(
    *,
    project_id: int,
    name: str,
    request: ProjectServiceUpsertRequest,
) -> ProjectService:
    return project_services_repository.upsert(
        project_id=project_id,
        name=name,
        description=request.description,
        price_text=request.price_text,
        tags=request.tags,
        duration_minutes=request.duration_minutes,
        working_hours=request.working_hours,
        service_days=request.service_days,
        date_exceptions=request.date_exceptions,
    )


@app.post("/api/projects/{project_id}/services")
async def api_project_services_upsert(
    project_id: int,
    request: ProjectServiceUpsertRequest,
    _principal: Annotated[str, Depends(require_internal_token)],
) -> dict[str, object]:
    try:
        stripped_name = validate_project_service(
            name=request.name,
            duration_minutes=request.duration_minutes,
            working_hours=request.working_hours,
            service_days=request.service_days,
            date_exceptions=request.date_exceptions,
        )
    except ProjectServiceValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from exc
    project_settings = await asyncio.to_thread(
        calendar_settings_repository.get, project_id
    )
    authorize_calendar_config(
        actor=request.actor,
        actor_role=request.actor_role,
        project_settings=project_settings,
    )
    lock = await acquire_service_upsert_lock(
        project_id=project_id, name=stripped_name
    )
    async with lock:
        service = await asyncio.to_thread(
            _upsert_project_service_call,
            project_id=project_id,
            name=stripped_name,
            request=request,
        )
    logger.info(
        "project_service_upserted",
        extra={
            "project_id": project_id,
            "service_id": service.id,
            "service_name": service.name,
        },
    )
    return _project_service_to_dict(service)


@app.delete("/api/projects/{project_id}/services/{service_id}")
async def api_project_services_delete(
    project_id: int,
    service_id: int,
    request: ProjectServiceDeleteRequest,
    _principal: Annotated[str, Depends(require_internal_token)],
) -> dict[str, object]:
    authorize_service_remove(actor_role=request.actor_role)
    # Resolve the row first so we can acquire the per-name lock; this also
    # gives us a single, uniform 404 path. Any race between this lookup and
    # the actual delete (e.g. an alias-path delete sneaking in) is harmless —
    # the SQL delete is a no-op and we still emit a structured success log,
    # but we re-check with another ``get`` to keep semantics tight.
    try:
        existing = await asyncio.to_thread(
            project_services_repository.get,
            project_id=project_id,
            service_id=service_id,
        )
    except ProjectServiceNotFound as exc:
        raise HTTPException(
            status_code=404, detail="project_service_not_found"
        ) from exc
    lock = await acquire_service_upsert_lock(
        project_id=project_id, name=existing.name
    )
    async with lock:
        try:
            await asyncio.to_thread(
                project_services_repository.delete,
                project_id=project_id,
                service_id=service_id,
            )
        except ProjectServiceNotFound as exc:
            # Tight race: the row was removed (by the alias path, which uses a
            # different lock) between our lookup and the delete. Map to 404
            # rather than leaking a 500 — same body as the lookup-miss path.
            raise HTTPException(
                status_code=404, detail="project_service_not_found"
            ) from exc
    logger.info(
        "project_service_deleted",
        extra={
            "project_id": project_id,
            "service_id": service_id,
            "service_name": existing.name,
        },
    )
    return {"deleted": True}


# --- /api/projects/{id}/services/nl-ops surface (Epic 13, story 13.04) ------
#
# Operator-driven natural-language proposal/confirm/cancel state machine on the
# canonical `project_services` catalog. The bot dispatcher (story 13.05) DMs
# previews + confirm-tokens; this api layer owns parsing, persistence, single-
# pending invariant, ownership-checked confirmation, and full-payload audit
# logging.


class ServicesNlOpProposeRequest(BaseModel):
    originating_operator: str
    text: str
    now: str | None = None


class ServicesNlOpConfirmRequest(BaseModel):
    presenter_operator: str
    confirm_token: str
    actor_role: str = "operator"
    now: str | None = None


class ServicesNlOpCancelRequest(BaseModel):
    presenter_operator: str
    now: str | None = None


def _parse_optional_now(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_now") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _nl_session_to_dict(session: ServicesNlSession) -> dict[str, object]:
    body: dict[str, object] = {
        "session_id": session.id,
        "project_id": session.project_id,
        "originating_operator": session.originating_operator,
        "op_type": session.op_type,
        "payload": session.payload,
        "preview": session.preview,
        "status": session.status,
        "created_at": session.created_at,
        "expires_at": session.expires_at,
        "consumed_at": session.consumed_at,
        "soft_deleted_at": session.soft_deleted_at,
    }
    if session.plaintext_confirm_token is not None:
        body["confirm_token"] = session.plaintext_confirm_token
    if session.prior_cancelled_session_ids:
        # First (and typically only) prior-pending session id that the
        # single-pending invariant flipped to cancelled by this propose.
        # The bot dispatcher (story 13.05) DMs a one-line notice before the
        # new preview so the operator knows the older draft is gone.
        body["prior_cancelled_session_id"] = session.prior_cancelled_session_ids[0]
    return body


@app.post("/api/projects/{project_id}/services/nl-ops")
async def api_services_nl_ops_propose(
    project_id: int,
    request: ServicesNlOpProposeRequest,
    _principal: Annotated[str, Depends(require_internal_token)],
) -> dict[str, object]:
    now = _parse_optional_now(request.now)
    session = await asyncio.to_thread(
        services_nl_ops_repository.propose,
        project_id=project_id,
        originating_operator=request.originating_operator,
        text=request.text,
        now=now,
    )
    return _nl_session_to_dict(session)


@app.post("/api/projects/{project_id}/services/nl-ops/{session_id}/confirm")
async def api_services_nl_ops_confirm(
    project_id: int,
    session_id: int,
    request: ServicesNlOpConfirmRequest,
    _principal: Annotated[str, Depends(require_internal_token)],
) -> dict[str, object]:
    now = _parse_optional_now(request.now)
    try:
        existing = await asyncio.to_thread(
            services_nl_ops_repository.get, session_id
        )
    except ServicesNlSessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session_not_found") from exc
    if existing.project_id != project_id:
        raise HTTPException(status_code=404, detail="session_not_found")
    # Permission split per FR-18/FR-21: admin can propose `удали услугу` but
    # the confirmation of a destructive op is operator-only.
    if existing.op_type == OP_SERVICE_REMOVE:
        authorize_service_remove(actor_role=request.actor_role)
    else:
        project_settings = await asyncio.to_thread(
            calendar_settings_repository.get, project_id
        )
        authorize_calendar_config(
            actor=request.presenter_operator,
            actor_role=request.actor_role,
            project_settings=project_settings,
        )
    try:
        consumed = await asyncio.to_thread(
            services_nl_ops_repository.consume,
            session_id=session_id,
            presenter_operator=request.presenter_operator,
            confirm_token=request.confirm_token,
            now=now,
        )
    except ServicesNlSessionNotOwner as exc:
        raise HTTPException(status_code=403, detail="not_session_owner") from exc
    except ServicesNlInvalidConfirmToken as exc:
        raise HTTPException(
            status_code=401, detail="invalid_confirm_token"
        ) from exc
    except ServicesNlSessionExpired as exc:
        raise HTTPException(status_code=410, detail="session_expired") from exc
    except ServicesNlSessionNotPending as exc:
        raise HTTPException(
            status_code=410, detail=f"session_not_pending:{exc}"
        ) from exc
    try:
        applied_id = await apply_confirmed(
            session=consumed,
            repo=project_services_repository,
            lock_factory=acquire_service_upsert_lock,
        )
    except ProjectServiceNotFound as exc:
        raise HTTPException(
            status_code=404, detail="project_service_not_found"
        ) from exc
    body = _nl_session_to_dict(consumed)
    body["applied_op_type"] = consumed.op_type
    body["applied_payload"] = consumed.payload
    body["applied_service_id"] = applied_id
    return body


@app.post("/api/projects/{project_id}/services/nl-ops/{session_id}/cancel")
async def api_services_nl_ops_cancel(
    project_id: int,
    session_id: int,
    request: ServicesNlOpCancelRequest,
    _principal: Annotated[str, Depends(require_internal_token)],
) -> dict[str, object]:
    now = _parse_optional_now(request.now)
    try:
        existing = await asyncio.to_thread(
            services_nl_ops_repository.get, session_id
        )
    except ServicesNlSessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session_not_found") from exc
    if existing.project_id != project_id:
        raise HTTPException(status_code=404, detail="session_not_found")
    try:
        cancelled = await asyncio.to_thread(
            services_nl_ops_repository.cancel,
            session_id=session_id,
            presenter_operator=request.presenter_operator,
            now=now,
        )
    except ServicesNlSessionNotOwner as exc:
        raise HTTPException(status_code=403, detail="not_session_owner") from exc
    except ServicesNlSessionNotPending as exc:
        raise HTTPException(
            status_code=410, detail=f"session_not_pending:{exc}"
        ) from exc
    return _nl_session_to_dict(cancelled)


@app.get("/api/projects/{project_id}/services/nl-ops/latest-pending")
async def api_services_nl_ops_latest_pending(
    project_id: int,
    operator: str,
    _principal: Annotated[str, Depends(require_internal_token)],
) -> dict[str, object]:
    session = await asyncio.to_thread(
        services_nl_ops_repository.latest_pending,
        project_id=project_id,
        operator=operator,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="no_pending")
    return _nl_session_to_dict(session)

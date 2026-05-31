from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    app_env: str = "development"
    log_level: str = "INFO"
    log_format: str = "text"
    api_port: int = 8000
    web_ui_port: int = 8001
    bot_gateway_port: int = 8002
    ingest_worker_port: int = 8003
    scheduler_port: int = 8004
    qdrant_url: str = "http://qdrant:6333"
    database_url: str = "postgresql://postgres:postgres@postgres:5432/semantaix"
    persistence_db_path: str = ".data/semantaix_story1.db"
    telegram_bot_token: str = "replace-me"
    telegram_bot_api_base_url: str = "https://api.telegram.org"
    telegram_bot_api_local_mode: bool = False
    telegram_bot_api_file_storage_root: str = "/var/lib/telegram-bot-api"
    telegram_api_id: str | None = None
    telegram_api_hash: str | None = None
    telegram_alert_username: str = "@ajdevy"
    telegram_alert_chat_id: str | None = None
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openai/gpt-4o-mini"
    openrouter_grounding_model: str = "google/gemini-2.0-flash-lite-001"
    # Story 12.30 — default sampling temperature for generative LLM calls (the
    # grounded answer, catalog digest). 0.0 = deterministic. The booking funnel's
    # structured extraction and the grounding verifier are hard-pinned to 0 in
    # OpenRouterClient regardless of this value; raise it only to add phrasing
    # variety to grounded answers.
    openrouter_temperature: float = 0.0
    incident_db_path: str = ".data/semantaix_incidents.db"
    incident_dedup_window_seconds: int = 300
    telegram_alert_debounce_seconds: int = 300
    hitl_ticket_db_path: str = ".data/semantaix_hitl.db"
    hitl_primary_operator_username: str = "@ajdevy"
    hitl_primary_operator_chat_id: str | None = None
    inbound_ack_message: str = (
        "Минутку, уточню и вернусь с ответом."
    )
    inbound_interim_message: str = "Проверяю, минуточку… 🙂"
    # Story 12.12 — scoping fields a booking must collect (comma list, canonical
    # order). Default drops tour-only `difficulty`/`drivers` so a plain rental
    # asks only date/people/buggies; tour projects re-add them via runtime config.
    scoping_required_fields: str = "dates,headcount,vehicle_count"
    # Story 12.13 — send the interim ack only if the answer pipeline hasn't
    # returned within this many seconds (so quick turns get no "минуточку").
    inbound_interim_delay_seconds: float = 8.0
    scope_decline_messages: str = (
        "Этим не занимаюсь.\n"
        "Не моя тема 🙂\n"
        "Не смогу тут помочь.\n"
        "Это не ко мне.\n"
        "С этим не помогу."
    )
    bot_persona_first_name: str = "Анна"
    bot_persona_last_name: str = "Иванова"
    bot_telegram_description: str = (
        "Здравствуйте! Напишите свой вопрос — постараюсь ответить здесь сразу. "
        "Если потребуется помощь коллег, передам им и вернусь к вам с ответом."
    )
    bot_telegram_short_description: str = "На связи в чате. Пишите ваш вопрос."
    api_internal_base_url: str = "http://api:8000"
    rag_grounding_score_threshold: float = 0.6
    default_language: str = "ru"
    default_country_code: str = "RU"
    default_timezone: str = "Europe/Moscow"
    default_location: str = "Moscow"
    weather_provider_base_url: str = "https://api.open-meteo.com"
    rag_db_path: str = ".data/semantaix_rag.db"
    knowledge_db_path: str = ".data/semantaix_knowledge.db"
    hitl_config_admin_username: str = "@ajdevy"
    answer_trace_db_path: str = ".data/semantaix_answer_traces.db"
    answer_trace_snippet_max_chars: int = 240
    nl_ops_enabled: bool = True
    nl_ops_db_path: str = ".data/semantaix_nl_ops.db"
    nl_ops_admin_user_ids: str = ""
    nl_ops_default_tenant_id: str = "default"
    backup_db_path: str = ".data/semantaix_backups.db"
    backup_archive_dir: str = ".data/backups"
    backup_source_paths: str = (
        ".data/semantaix_story1.db,"
        ".data/semantaix_incidents.db,"
        ".data/semantaix_hitl.db,"
        ".data/semantaix_rag.db,"
        ".data/semantaix_knowledge.db,"
        ".data/semantaix_web_auth.db,"
        ".data/semantaix_operator_files.db"
    )
    operator_upload_max_bytes: int = 20 * 1024 * 1024
    operator_upload_max_audio_seconds: int = 900
    operator_upload_pdf_ocr_max_pages: int = 50
    operator_upload_storage_dir: str = ".data/operator_uploads"
    # Story 12.05 — registered sales materials live under this directory,
    # one subfolder per project_id (e.g. ``.data/sales_materials/7/abc.mp4``).
    sales_materials_storage_dir: str = ".data/sales_materials"
    operator_kb_intent_phrases_path: str = "data/russian_kb_intent_phrases.txt"
    operator_upload_api_timeout_seconds: int = 120
    operator_kb_session_ttl_seconds: int = 600
    operator_media_group_debounce_seconds: float = 3.0
    operator_media_group_settling_cap_seconds: float = 30.0
    operator_media_group_poll_interval_seconds: float = 0.5
    # Offline message backlog recovery: a customer message whose Telegram send
    # time is older than `stale_seconds` is treated as redelivered backlog and
    # buffered per chat; the debounced flush answers only the latest message,
    # pulling up to `max_context_messages` preceding ones in as context when the
    # latest is shorter than `min_context_chars` (or a known context cue).
    offline_backlog_stale_seconds: float = 30.0
    offline_backlog_debounce_seconds: float = 5.0
    offline_backlog_settling_cap_seconds: float = 30.0
    offline_backlog_poll_interval_seconds: float = 0.5
    offline_backlog_min_context_chars: int = 12
    offline_backlog_max_context_messages: int = 3
    # Backoff delays (seconds, comma list) for retrying a failed inbound forward
    # before it strands in the pending-forward outbox — survives a transient api
    # outage (e.g. the api recreating during a rebuild) instead of dropping the
    # customer's message until the next restart.
    pending_forward_retry_delays_seconds: str = "2,5,10"
    # Story 12.24 — timeout (seconds) for the bot_gateway → api inbound forward.
    # A sales-escalation turn blocks the api response on synchronous Telegram
    # sends and can exceed the 10s default, making bot_gateway time out and retry
    # mid-flight (the live duplicate-send trigger). Set well above worst-case
    # turn time; the api-side claim (Story 12.24) still dedups any retry that
    # does occur. The webhook already 200s to Telegram first, so a long forward
    # timeout never delays Telegram.
    inbound_forward_timeout_seconds: int = 45
    operator_files_db_path: str = ".data/semantaix_operator_files.db"
    operator_files_list_default_limit: int = 10
    operator_files_list_max_limit: int = 50
    web_auth_db_path: str = ".data/semantaix_web_auth.db"
    web_session_cookie_name: str = "semantaix_session"
    web_session_cookie_secure: bool = True
    internal_service_token: str | None = None
    faster_whisper_model_size: str = "base"
    faster_whisper_compute_type: str = "int8"
    faster_whisper_cache_dir: str = "/app/.cache/whisper"
    projects_db_path: str = ".data/semantaix_projects.db"
    operators_db_path: str = ".data/semantaix_operators.db"
    sales_db_path: str = ".data/semantaix_sales.db"
    admin_session_db_path: str = ".data/semantaix_admin_sessions.db"
    admin_telegram_username: str = "@ajdevy"
    admin_login_code_ttl_seconds: int = 300
    admin_session_ttl_seconds: int = 86400
    admin_internal_token: str = ""
    web_ui_admin_cookie_secure: bool = False
    calendar_db_path: str = ".data/semantaix_calendar.db"
    calendar_token_encryption_key: str | None = None
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str | None = None
    calendar_oauth_state_ttl_seconds: int = 300
    calendar_http_timeout_seconds: float = 10.0
    services_nl_op_session_ttl_seconds: int = 600

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    def backup_source_path_list(self) -> list[str]:
        return [path.strip() for path in self.backup_source_paths.split(",") if path.strip()]

    def nl_ops_admin_user_id_list(self) -> list[str]:
        return [item.strip() for item in self.nl_ops_admin_user_ids.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings()

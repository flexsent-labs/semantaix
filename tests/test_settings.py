from platform_common.settings import AppSettings


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ALERT_USERNAME", raising=False)
    settings = AppSettings(_env_file=None)
    assert settings.app_env == "development"
    assert settings.log_level == "INFO"
    assert settings.api_port == 8000
    assert settings.persistence_db_path == ".data/semantaix_story1.db"
    assert settings.openrouter_base_url == "https://openrouter.ai/api/v1"
    assert settings.incident_db_path == ".data/semantaix_incidents.db"
    assert settings.incident_dedup_window_seconds == 300
    assert settings.telegram_alert_username == "@ajdevy"
    assert settings.telegram_alert_chat_id is None
    assert settings.telegram_alert_debounce_seconds == 300
    assert settings.hitl_ticket_db_path == ".data/semantaix_hitl.db"
    assert settings.rag_db_path == ".data/semantaix_rag.db"
    assert settings.knowledge_db_path == ".data/semantaix_knowledge.db"
    assert settings.hitl_config_admin_username == "@ajdevy"
    assert settings.answer_trace_db_path == ".data/semantaix_answer_traces.db"
    assert settings.answer_trace_snippet_max_chars == 240
    assert settings.nl_ops_enabled is True
    assert settings.nl_ops_db_path == ".data/semantaix_nl_ops.db"
    assert settings.nl_ops_admin_user_id_list() == []
    assert settings.nl_ops_default_tenant_id == "default"
    assert settings.backup_db_path == ".data/semantaix_backups.db"
    assert settings.backup_archive_dir == ".data/backups"
    assert settings.backup_source_path_list() == [
        ".data/semantaix_story1.db",
        ".data/semantaix_incidents.db",
        ".data/semantaix_hitl.db",
        ".data/semantaix_rag.db",
        ".data/semantaix_knowledge.db",
        ".data/semantaix_web_auth.db",
        ".data/semantaix_operator_files.db",
    ]
    assert settings.bot_persona_first_name == "Анна"
    assert settings.bot_persona_last_name == "Иванова"
    assert "бот" not in settings.bot_telegram_description.lower()
    assert "бот" not in settings.bot_telegram_short_description.lower()
    assert settings.inbound_ack_message == "Минутку, уточню и вернусь с ответом."
    assert settings.web_auth_db_path == ".data/semantaix_web_auth.db"
    assert settings.web_session_cookie_name == "semantaix_session"
    assert settings.web_session_cookie_secure is True
    assert settings.internal_service_token is None


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("API_PORT", "9000")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
    monkeypatch.setenv("PERSISTENCE_DB_PATH", ".tmp/test.sqlite3")
    monkeypatch.setenv("INCIDENT_DB_PATH", ".tmp/incidents.sqlite3")
    monkeypatch.setenv("INCIDENT_DEDUP_WINDOW_SECONDS", "60")
    monkeypatch.setenv("TELEGRAM_ALERT_USERNAME", "@ops")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-1001234")
    monkeypatch.setenv("TELEGRAM_ALERT_DEBOUNCE_SECONDS", "120")
    monkeypatch.setenv("HITL_TICKET_DB_PATH", ".tmp/hitl.sqlite3")
    monkeypatch.setenv("RAG_DB_PATH", ".tmp/rag.sqlite3")
    monkeypatch.setenv("KNOWLEDGE_DB_PATH", ".tmp/knowledge.sqlite3")
    monkeypatch.setenv("HITL_CONFIG_ADMIN_USERNAME", "@admin")
    monkeypatch.setenv("ANSWER_TRACE_DB_PATH", ".tmp/answer_traces.sqlite3")
    monkeypatch.setenv("ANSWER_TRACE_SNIPPET_MAX_CHARS", "120")
    monkeypatch.setenv("NL_OPS_ENABLED", "false")
    monkeypatch.setenv("NL_OPS_DB_PATH", ".tmp/nl_ops.sqlite3")
    monkeypatch.setenv("NL_OPS_ADMIN_USER_IDS", " 111 , 222 , ")
    monkeypatch.setenv("NL_OPS_DEFAULT_TENANT_ID", "tenant-x")
    monkeypatch.setenv("BACKUP_DB_PATH", ".tmp/backups.sqlite3")
    monkeypatch.setenv("BACKUP_ARCHIVE_DIR", ".tmp/backups")
    monkeypatch.setenv("BACKUP_SOURCE_PATHS", " .tmp/a.db , .tmp/b.db , ")
    monkeypatch.setenv("BOT_PERSONA_FIRST_NAME", "Мария")
    monkeypatch.setenv("BOT_PERSONA_LAST_NAME", "Петрова")
    monkeypatch.setenv("BOT_TELEGRAM_DESCRIPTION", "Здравствуйте, я на связи.")
    monkeypatch.setenv("BOT_TELEGRAM_SHORT_DESCRIPTION", "Пишите вопрос.")
    settings = AppSettings(_env_file=None)
    assert settings.app_env == "test"
    assert settings.log_level == "DEBUG"
    assert settings.api_port == 9000
    assert settings.persistence_db_path == ".tmp/test.sqlite3"
    assert settings.openrouter_model == "anthropic/claude-3.5-sonnet"
    assert settings.incident_db_path == ".tmp/incidents.sqlite3"
    assert settings.incident_dedup_window_seconds == 60
    assert settings.telegram_alert_username == "@ops"
    assert settings.telegram_alert_chat_id == "-1001234"
    assert settings.telegram_alert_debounce_seconds == 120
    assert settings.hitl_ticket_db_path == ".tmp/hitl.sqlite3"
    assert settings.rag_db_path == ".tmp/rag.sqlite3"
    assert settings.knowledge_db_path == ".tmp/knowledge.sqlite3"
    assert settings.hitl_config_admin_username == "@admin"
    assert settings.answer_trace_db_path == ".tmp/answer_traces.sqlite3"
    assert settings.answer_trace_snippet_max_chars == 120
    assert settings.nl_ops_enabled is False
    assert settings.nl_ops_db_path == ".tmp/nl_ops.sqlite3"
    assert settings.nl_ops_admin_user_id_list() == ["111", "222"]
    assert settings.nl_ops_default_tenant_id == "tenant-x"
    assert settings.backup_db_path == ".tmp/backups.sqlite3"
    assert settings.backup_archive_dir == ".tmp/backups"
    assert settings.backup_source_path_list() == [".tmp/a.db", ".tmp/b.db"]
    assert settings.bot_persona_first_name == "Мария"
    assert settings.bot_persona_last_name == "Петрова"
    assert settings.bot_telegram_description == "Здравствуйте, я на связи."
    assert settings.bot_telegram_short_description == "Пишите вопрос."

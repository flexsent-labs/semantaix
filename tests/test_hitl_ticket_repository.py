import sqlite3

from services.api.app.hitl import HitlTicketRepository


def test_hitl_ticket_lifecycle(tmp_path):
    repository = HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))

    created = repository.create(conversation_ref="conv-1", reason="low_confidence")
    assert created.status == "open"
    assert created.operator_username is None
    assert created.target_chat_id is None

    assigned = repository.assign(ticket_id=created.id, operator_username="@ajdevy")
    assert assigned.status == "assigned"
    assert assigned.operator_username == "@ajdevy"

    resolved = repository.resolve(ticket_id=created.id)
    assert resolved.status == "resolved"
    assert resolved.resolved_at is not None

    items = repository.list_all()
    assert len(items) == 1
    assert items[0].id == created.id
    fetched = repository.get(created.id)
    assert fetched.id == created.id


def test_hitl_ticket_stores_target_chat_id(tmp_path):
    repository = HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))
    created = repository.create(
        conversation_ref="conv-7",
        reason="uncertain",
        target_chat_id=123456,
    )
    assert created.target_chat_id == 123456


def test_hitl_schema_migration_adds_target_chat_id(tmp_path):
    db_path = tmp_path / "legacy_hitl.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE hitl_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_ref TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            operator_username TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """
    )
    connection.commit()
    connection.close()

    repository = HitlTicketRepository(str(db_path))
    created = repository.create(conversation_ref="conv-legacy", reason="migration")
    assert created.target_chat_id is None


def test_runtime_config_round_trip(tmp_path):
    repository = HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))
    repository.set_runtime_config(key="foo", value="bar", updated_by="@ajdevy")
    assert repository.get_runtime_config("foo") == "bar"
    # Overwrite preserves the latest value.
    repository.set_runtime_config(key="foo", value="baz", updated_by="@admin")
    assert repository.get_runtime_config("foo") == "baz"
    assert repository.get_runtime_config("missing") is None


def test_get_bot_persona_falls_back_to_defaults(tmp_path):
    repository = HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))
    persona = repository.get_bot_persona(
        default_first_name="Анна", default_last_name="Иванова"
    )
    assert persona == ("Анна", "Иванова")


def test_get_bot_persona_returns_runtime_override(tmp_path):
    repository = HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))
    repository.set_runtime_config(
        key="bot_persona_first_name", value="Мария", updated_by="@ajdevy"
    )
    repository.set_runtime_config(
        key="bot_persona_last_name", value="Петрова", updated_by="@ajdevy"
    )
    persona = repository.get_bot_persona(
        default_first_name="Анна", default_last_name="Иванова"
    )
    assert persona == ("Мария", "Петрова")


def test_get_bot_persona_partial_override(tmp_path):
    """Only first name overridden → last name falls back to default."""
    repository = HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))
    repository.set_runtime_config(
        key="bot_persona_first_name", value="Иван", updated_by="@ajdevy"
    )
    persona = repository.get_bot_persona(
        default_first_name="Анна", default_last_name="Иванова"
    )
    assert persona == ("Иван", "Иванова")


def test_get_bot_persona_preserves_cleared_surname(tmp_path):
    """An explicitly-stored empty surname survives — no default re-applied.

    Regression: setting the persona to a first name only stores ``""`` for
    the surname; a falsy ``or default`` read used to resurrect "Иванова" on
    every restart.
    """
    repository = HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))
    repository.set_runtime_config(
        key="bot_persona_first_name", value="Анна", updated_by="@ajdevy"
    )
    repository.set_runtime_config(
        key="bot_persona_last_name", value="", updated_by="@ajdevy"
    )
    persona = repository.get_bot_persona(
        default_first_name="Анна", default_last_name="Иванова"
    )
    assert persona == ("Анна", "")


def test_find_active_for_chat_returns_none_when_no_tickets(tmp_path):
    repository = HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))
    assert repository.find_active_for_chat(9001) is None


def test_find_active_for_chat_returns_assigned_ticket(tmp_path):
    repository = HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))
    ticket = repository.create(
        conversation_ref="q", reason="awaiting_human_response", target_chat_id=9001
    )
    repository.assign(ticket_id=ticket.id, operator_username="@op")
    found = repository.find_active_for_chat(9001)
    assert found is not None
    assert found.id == ticket.id
    assert found.status == "assigned"


def test_find_active_for_chat_returns_open_ticket(tmp_path):
    repository = HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))
    ticket = repository.create(
        conversation_ref="q", reason="r", target_chat_id=9001
    )
    found = repository.find_active_for_chat(9001)
    assert found is not None
    assert found.id == ticket.id


def test_find_active_for_chat_skips_resolved_tickets(tmp_path):
    repository = HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))
    old = repository.create(
        conversation_ref="q-old", reason="r", target_chat_id=9001
    )
    repository.assign(ticket_id=old.id, operator_username="@op")
    repository.resolve(ticket_id=old.id)
    # After resolution there is no active ticket for that chat.
    assert repository.find_active_for_chat(9001) is None


def test_find_active_for_chat_returns_newest_when_multiple_active(tmp_path):
    repository = HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))
    first = repository.create(conversation_ref="q1", reason="r", target_chat_id=9001)
    second = repository.create(conversation_ref="q2", reason="r", target_chat_id=9001)
    found = repository.find_active_for_chat(9001)
    assert found is not None
    assert found.id == second.id
    assert first.id != second.id


def test_list_active_for_operator_filters_by_status_and_username(tmp_path):
    repository = HitlTicketRepository(str(tmp_path / "hitl.sqlite3"))
    a = repository.create(conversation_ref="qa", reason="r", target_chat_id=1)
    b = repository.create(conversation_ref="qb", reason="r", target_chat_id=2)
    c = repository.create(conversation_ref="qc", reason="r", target_chat_id=3)
    repository.assign(ticket_id=a.id, operator_username="@op")
    repository.assign(ticket_id=b.id, operator_username="@op")
    repository.assign(ticket_id=c.id, operator_username="@other")

    open_for_op = repository.list_active_for_operator("@op")
    ids = {t.id for t in open_for_op}
    assert ids == {a.id, b.id}

    # Resolving one drops it out of the list.
    repository.resolve(ticket_id=a.id)
    remaining_ids = {t.id for t in repository.list_active_for_operator("@op")}
    assert remaining_ids == {b.id}

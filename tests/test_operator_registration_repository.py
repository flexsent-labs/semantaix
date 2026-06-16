from __future__ import annotations

import pytest

from services.api.app.operator_registration import (
    OperatorRegistrationRepository,
    RegistrationAlreadyProcessed,
    RegistrationCooldownActive,
    RegistrationNotFound,
    RegistrationPendingConflict,
)
from services.api.app.operators import OperatorRepository, OperatorUsernameConflict


def _repos(tmp_path):
    db_path = str(tmp_path / "operators.sqlite3")
    operator_repo = OperatorRepository(db_path)
    registration_repo = OperatorRegistrationRepository(db_path)
    return operator_repo, registration_repo


def test_create_request_normalizes_username_and_gets_by_status(tmp_path):
    _, registration_repo = _repos(tmp_path)
    created = registration_repo.create_request(
        username="new_op",
        chat_id=101,
        display_name="New Operator",
    )
    assert created.username == "@new_op"
    assert created.status == "pending"
    fetched = registration_repo.get(created.id)
    assert fetched is not None
    assert fetched.display_name == "New Operator"
    pending = registration_repo.list_by_status("pending")
    assert [item.id for item in pending] == [created.id]


def test_create_request_rejects_pending_duplicate(tmp_path):
    _, registration_repo = _repos(tmp_path)
    registration_repo.create_request(username="@dup", chat_id=10)
    with pytest.raises(RegistrationPendingConflict):
        registration_repo.create_request(username="@dup", chat_id=10)


def test_create_request_rejects_when_cooldown_active(tmp_path):
    _, registration_repo = _repos(tmp_path)
    created = registration_repo.create_request(username="@cooldown", chat_id=77)
    registration_repo.reject(request_id=created.id, reviewed_by="@admin")
    with pytest.raises(RegistrationCooldownActive):
        registration_repo.create_request(username="@cooldown", chat_id=77)


def test_get_returns_none_for_unknown_request(tmp_path):
    _, registration_repo = _repos(tmp_path)
    assert registration_repo.get(99999) is None


def test_approve_creates_operator_and_marks_request(tmp_path):
    operator_repo, registration_repo = _repos(tmp_path)
    request = registration_repo.create_request(
        username="@approve_me",
        chat_id=999,
        display_name="Approve Me",
    )
    operator = registration_repo.approve(
        request_id=request.id,
        reviewed_by="@admin",
        project_id=7,
        operator_repository=operator_repo,
    )
    assert operator.username == "@approve_me"
    assert operator.chat_id == 999
    assert operator.project_id == 7
    updated = registration_repo.get(request.id)
    assert updated is not None
    assert updated.status == "approved"
    assert updated.project_id == 7
    assert updated.reviewed_by == "@admin"
    events = registration_repo.list_onboarding_events(operator_id=operator.id)
    assert [event_type for event_type, _ in events] == ["approved"]


def test_approve_raises_not_found(tmp_path):
    operator_repo, registration_repo = _repos(tmp_path)
    with pytest.raises(RegistrationNotFound):
        registration_repo.approve(
            request_id=1,
            reviewed_by="@admin",
            project_id=1,
            operator_repository=operator_repo,
        )


def test_approve_raises_already_processed(tmp_path):
    operator_repo, registration_repo = _repos(tmp_path)
    request = registration_repo.create_request(username="@processed", chat_id=1)
    registration_repo.reject(request_id=request.id, reviewed_by="@admin")
    with pytest.raises(RegistrationAlreadyProcessed):
        registration_repo.approve(
            request_id=request.id,
            reviewed_by="@admin",
            project_id=1,
            operator_repository=operator_repo,
        )


def test_approve_raises_operator_username_conflict(tmp_path):
    operator_repo, registration_repo = _repos(tmp_path)
    operator_repo.create(username="@taken", project_id=3, chat_id=1)
    request = registration_repo.create_request(username="@taken", chat_id=2)
    with pytest.raises(OperatorUsernameConflict):
        registration_repo.approve(
            request_id=request.id,
            reviewed_by="@admin",
            project_id=3,
            operator_repository=operator_repo,
        )


def test_reject_sets_status_and_cooldown(tmp_path):
    _, registration_repo = _repos(tmp_path)
    request = registration_repo.create_request(username="@reject_me", chat_id=12)
    updated = registration_repo.reject(request_id=request.id, reviewed_by="@admin")
    assert updated.status == "rejected"
    assert updated.reviewed_by == "@admin"
    assert updated.rejection_cooldown_until is not None


def test_reject_not_found_and_already_processed(tmp_path):
    operator_repo, registration_repo = _repos(tmp_path)
    with pytest.raises(RegistrationNotFound):
        registration_repo.reject(request_id=123, reviewed_by="@admin")
    request = registration_repo.create_request(username="@approved_later", chat_id=22)
    registration_repo.approve(
        request_id=request.id,
        reviewed_by="@admin",
        project_id=11,
        operator_repository=operator_repo,
    )
    with pytest.raises(RegistrationAlreadyProcessed):
        registration_repo.reject(request_id=request.id, reviewed_by="@admin")


def test_record_onboarding_event_round_trip(tmp_path):
    operator_repo, registration_repo = _repos(tmp_path)
    operator = operator_repo.create(username="@events", project_id=4, chat_id=123)
    registration_repo.record_onboarding_event(
        operator_id=operator.id,
        event_type="onboarding_sent",
    )
    events = registration_repo.list_onboarding_events(operator_id=operator.id)
    assert len(events) == 1
    assert events[0][0] == "onboarding_sent"

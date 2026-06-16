from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services.api.app.operator_registration import (
    OperatorRegistrationRepository,
    RegistrationAlreadyProcessed,
    RegistrationCooldownActive,
    RegistrationNotFound,
    RegistrationPendingConflict,
    RegistrationRequest,
)
from services.api.app.operators import OperatorRepository, OperatorUsernameConflict

router = APIRouter(tags=["operator-registration"])


class RegisterRequestBody(BaseModel):
    username: str
    chat_id: int
    display_name: str | None = None


class ApproveRegistrationBody(BaseModel):
    project_id: int | None = None


class OnboardingEventBody(BaseModel):
    event_type: str


def _registration_to_dict(request: RegistrationRequest) -> dict[str, object]:
    return {
        "id": request.id,
        "username": request.username,
        "chat_id": request.chat_id,
        "display_name": request.display_name,
        "status": request.status,
        "project_id": request.project_id,
        "created_at": request.created_at,
        "reviewed_at": request.reviewed_at,
        "reviewed_by": request.reviewed_by,
        "rejection_cooldown_until": request.rejection_cooldown_until,
    }


def wire_operator_registration_routes(
    *,
    app_router: APIRouter,
    registration_repository: OperatorRegistrationRepository,
    operator_repository: OperatorRepository,
    require_internal_token,
    require_admin_or_internal,
    operator_to_dict,
    ensure_default_project_id,
    notify_admin_new_request,
    send_onboarding_dm,
    record_onboarding_event,
) -> None:
    @app_router.post("/operators/register-request")
    async def create_register_request(
        body: RegisterRequestBody,
        _principal: str = Depends(require_internal_token),
    ) -> dict[str, object]:
        existing = operator_repository.find_by_username(body.username)
        if existing is not None:
            raise HTTPException(status_code=409, detail="already_operator")
        try:
            request = registration_repository.create_request(
                username=body.username,
                chat_id=body.chat_id,
                display_name=body.display_name,
            )
        except RegistrationPendingConflict as exc:
            raise HTTPException(
                status_code=409, detail="registration_pending"
            ) from exc
        except RegistrationCooldownActive as exc:
            raise HTTPException(
                status_code=409, detail="registration_cooldown"
            ) from exc
        await notify_admin_new_request(request)
        return {"request_id": request.id, "status": request.status}

    @app_router.get("/operators/register-requests")
    def list_register_requests(
        status: str = "pending",
        _principal: str = Depends(require_admin_or_internal),
    ) -> dict[str, object]:
        items = registration_repository.list_by_status(status)
        return {"items": [_registration_to_dict(item) for item in items]}

    @app_router.post("/operators/register-requests/{request_id}/approve")
    async def approve_register_request(
        request_id: int,
        body: ApproveRegistrationBody,
        principal: str = Depends(require_admin_or_internal),
    ) -> dict[str, object]:
        project_id = body.project_id or ensure_default_project_id()
        reviewed_by = principal if principal != "internal" else "internal"
        try:
            operator = registration_repository.approve(
                request_id=request_id,
                reviewed_by=reviewed_by,
                project_id=project_id,
                operator_repository=operator_repository,
            )
        except RegistrationNotFound as exc:
            raise HTTPException(status_code=404, detail="request_not_found") from exc
        except RegistrationAlreadyProcessed as exc:
            raise HTTPException(status_code=409, detail="request_not_pending") from exc
        except OperatorUsernameConflict as exc:
            raise HTTPException(
                status_code=409, detail="operator_username_conflict"
            ) from exc
        await send_onboarding_dm(operator=operator, request_id=request_id)
        return operator_to_dict(operator)

    @app_router.post("/operators/register-requests/{request_id}/reject")
    def reject_register_request(
        request_id: int,
        principal: str = Depends(require_admin_or_internal),
    ) -> dict[str, object]:
        reviewed_by = principal if principal != "internal" else "internal"
        try:
            request = registration_repository.reject(
                request_id=request_id,
                reviewed_by=reviewed_by,
            )
        except RegistrationNotFound as exc:
            raise HTTPException(status_code=404, detail="request_not_found") from exc
        except RegistrationAlreadyProcessed as exc:
            raise HTTPException(status_code=409, detail="request_not_pending") from exc
        return _registration_to_dict(request)

    @app_router.post("/operators/register-requests/{request_id}/onboarding-notify")
    async def onboarding_notify(
        request_id: int,
        _principal: str = Depends(require_internal_token),
    ) -> dict[str, object]:
        request = registration_repository.get(request_id)
        if request is None or request.status != "approved":
            raise HTTPException(status_code=404, detail="request_not_approved")
        operator = operator_repository.find_by_username(request.username)
        if operator is None:
            raise HTTPException(status_code=404, detail="operator_not_found")
        await send_onboarding_dm(operator=operator, request_id=request_id)
        return {"operator_id": operator.id, "notified": True}

    @app_router.post("/operators/{operator_id}/onboarding-events")
    def post_onboarding_event(
        operator_id: int,
        body: OnboardingEventBody,
        _principal: str = Depends(require_internal_token),
    ) -> dict[str, object]:
        registration_repository.record_onboarding_event(
            operator_id=operator_id,
            event_type=body.event_type,
        )
        return {"operator_id": operator_id, "event_type": body.event_type}

    @app_router.get("/operators/id/{operator_id}")
    def get_operator_by_id(
        operator_id: int,
        _principal: str = Depends(require_internal_token),
    ) -> dict[str, object]:
        operator = operator_repository.list_all()
        match = next((op for op in operator if op.id == operator_id), None)
        if match is None:
            raise HTTPException(status_code=404, detail="operator_not_found")
        return operator_to_dict(match)

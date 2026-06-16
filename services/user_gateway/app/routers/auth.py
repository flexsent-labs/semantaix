"""QR auth HTTP endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from services.user_gateway.app.telegram_auth import TelethonAuthService

router = APIRouter(prefix="/auth", tags=["auth"])


class Verify2FaRequest(BaseModel):
    password: str


def get_auth_service() -> TelethonAuthService:
    from services.user_gateway.app.main import auth_service

    return auth_service


@router.post("/qr_start")
async def qr_start(
    operator_id: Annotated[int | None, Query()] = None,
    service: TelethonAuthService = Depends(get_auth_service),
) -> dict[str, object]:
    return await service.qr_start(operator_id=operator_id)


@router.get("/status")
def auth_status(
    operator_id: Annotated[int | None, Query()] = None,
    service: TelethonAuthService = Depends(get_auth_service),
) -> dict[str, object]:
    return service.get_status(operator_id=operator_id)


@router.post("/verify_2fa")
async def verify_2fa(
    body: Verify2FaRequest,
    operator_id: Annotated[int | None, Query()] = None,
    service: TelethonAuthService = Depends(get_auth_service),
) -> dict[str, str]:
    # Never log body.password — passed straight to Telethon sign_in.
    return await service.verify_2fa(body.password, operator_id=operator_id)

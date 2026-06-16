"""Internal message send endpoint for api outbound delivery."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from platform_common.settings import get_settings

router = APIRouter(prefix="/messages", tags=["messages"])


class SendMessageRequest(BaseModel):
    operator_id: int
    chat_id: int
    text: str


def require_internal_token(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    expected = get_settings().internal_service_token
    if (
        expected
        and authorization
        and authorization.startswith("Bearer ")
        and hmac.compare_digest(authorization.removeprefix("Bearer "), expected)
    ):
        return "internal"
    raise HTTPException(status_code=401, detail="internal_auth_required")


def get_client_pool():
    from services.user_gateway.app.main import operator_client_pool

    return operator_client_pool


@router.post("/send")
async def send_message(
    body: SendMessageRequest,
    _: Annotated[str, Depends(require_internal_token)] = "",
    pool=Depends(get_client_pool),
) -> dict[str, str]:
    if not pool.is_connected(body.operator_id):
        raise HTTPException(status_code=404, detail="operator_not_connected")
    await pool.send_message(body.operator_id, chat_id=body.chat_id, text=body.text)
    return {"status": "sent"}

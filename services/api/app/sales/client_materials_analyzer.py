"""``ClientMaterialsAnalyzer`` — LLM client-sendability judge (Story 12.05b).

After a successful KB ingest the bot_gateway calls
``POST /sales/materials/analyze-kb-file`` which dispatches into this
analyzer. We read the file metadata + extracted text via the existing
``operator_files_view`` (no new fetch/store helpers), clip the text to
the first 4000 chars, and ask the LLM whether the document looks like
something we can forward to a prospective customer.

Confidential files short-circuit before the LLM call. Any LLM error or
schema violation is swallowed and reported via ``AnalysisOutcome.reason``;
the bot_gateway hook treats both as "no extra message" — KB ingest itself
is never blocked.

The file's extracted text MUST NEVER appear in our own log lines (it is
sent only to the LLM in the user message). Tests assert that invariant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from services.api.app.openrouter_client import OpenRouterJsonSchemaViolation
from services.api.app.operator_files_view import KbFileMaterialView

logger = logging.getLogger(__name__)

EXTRACTED_TEXT_CAP = 4000

_ALLOWED_KINDS: frozenset[str] = frozenset({"video", "photo", "pdf", "document"})
_CAPTION_MAX_CHARS = 120

_SYSTEM_PROMPT_PATH = Path(__file__).parent / "system_prompts" / "sales_kb_material_analyzer.txt"


@dataclass(frozen=True)
class AnalysisOutcome:
    registered: bool
    material_id: int | None
    reason: str


class _OperatorFilesView(Protocol):
    def get_for_kb_material(
        self, *, short_id: str
    ) -> KbFileMaterialView | None: ...


class _OpenRouterClient(Protocol):
    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]: ...


class _MaterialsRepo(Protocol):
    def add(
        self,
        *,
        project_id: int,
        kind: str,
        local_path: str,
        byte_size: int,
        now: datetime,
        duration_seconds: int | None = ...,
        caption: str | None = ...,
        tags: list[str] | None = ...,
        telegram_file_id: str | None = ...,
        source_operator_file_id: str | None = ...,
    ) -> int: ...


def _load_system_prompt() -> str:
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _format_user_prompt(
    *, view: KbFileMaterialView, clipped_text: str
) -> str:
    return (
        "Метаданные файла:\n"
        f"- расширение: {view.file_extension}\n"
        f"- mime: {view.mime_type or 'unknown'}\n"
        f"- размер: {view.byte_size} байт\n"
        "\n"
        "Извлечённый текст (обрезан до первых нескольких страниц):\n"
        f"{clipped_text}"
    )


class ClientMaterialsAnalyzer:
    def __init__(
        self,
        *,
        openrouter: _OpenRouterClient,
        operator_files_view: _OperatorFilesView,
        materials_repo: _MaterialsRepo,
    ) -> None:
        self._openrouter = openrouter
        self._files = operator_files_view
        self._repo = materials_repo

    async def analyze_and_register(
        self,
        *,
        project_id: int,
        operator_file_short_id: str,
        now: datetime,
    ) -> AnalysisOutcome:
        view = self._files.get_for_kb_material(
            short_id=operator_file_short_id
        )
        if view is None:
            logger.info(
                "sales_kb_material_skipped_missing_file",
                extra={
                    "operator_file_short_id": operator_file_short_id,
                    "project_id": project_id,
                },
            )
            return AnalysisOutcome(
                registered=False,
                material_id=None,
                reason="operator_file_not_found",
            )

        if view.is_confidential:
            logger.info(
                "sales_kb_material_skipped_confidential",
                extra={
                    "operator_file_short_id": operator_file_short_id,
                    "project_id": project_id,
                },
            )
            return AnalysisOutcome(
                registered=False,
                material_id=None,
                reason="confidential_kb_file",
            )

        if not view.local_path:
            logger.info(
                "sales_kb_material_skipped_no_local_path",
                extra={
                    "operator_file_short_id": operator_file_short_id,
                    "project_id": project_id,
                },
            )
            return AnalysisOutcome(
                registered=False,
                material_id=None,
                reason="no_local_path",
            )

        if not view.extracted_text or not view.extracted_text.strip():
            logger.info(
                "sales_kb_material_skipped_empty_text",
                extra={
                    "operator_file_short_id": operator_file_short_id,
                    "project_id": project_id,
                },
            )
            return AnalysisOutcome(
                registered=False,
                material_id=None,
                reason="empty_extracted_text",
            )

        clipped = view.extracted_text[:EXTRACTED_TEXT_CAP]
        system = _load_system_prompt()
        user = _format_user_prompt(view=view, clipped_text=clipped)

        try:
            payload = await self._openrouter.complete_json(
                system=system, user=user
            )
        except OpenRouterJsonSchemaViolation:
            logger.warning(
                "sales_kb_material_schema_violation",
                extra={
                    "operator_file_short_id": operator_file_short_id,
                    "project_id": project_id,
                    "stage": "non_json_response",
                },
            )
            return AnalysisOutcome(
                registered=False,
                material_id=None,
                reason="llm_schema_violation",
            )
        except Exception as exc:  # broad: never propagate into bot_gateway
            logger.warning(
                "sales_kb_material_llm_error",
                extra={
                    "operator_file_short_id": operator_file_short_id,
                    "project_id": project_id,
                    "error": repr(exc),
                },
            )
            return AnalysisOutcome(
                registered=False,
                material_id=None,
                reason="llm_error",
            )

        sendable_raw = payload.get("sendable")
        kind_raw = payload.get("suggested_kind")
        if (
            not isinstance(sendable_raw, bool)
            or not isinstance(kind_raw, str)
            or kind_raw not in _ALLOWED_KINDS
        ):
            logger.warning(
                "sales_kb_material_schema_violation",
                extra={
                    "operator_file_short_id": operator_file_short_id,
                    "project_id": project_id,
                    "stage": "missing_required_fields",
                },
            )
            return AnalysisOutcome(
                registered=False,
                material_id=None,
                reason="llm_schema_violation",
            )

        reason_raw = payload.get("reason")
        reason = (
            reason_raw.strip()
            if isinstance(reason_raw, str) and reason_raw.strip()
            else ("sendable" if sendable_raw else "not_sendable")
        )

        if not sendable_raw:
            logger.info(
                "sales_kb_material_not_sendable",
                extra={
                    "operator_file_short_id": operator_file_short_id,
                    "project_id": project_id,
                    "reason": reason,
                },
            )
            return AnalysisOutcome(
                registered=False, material_id=None, reason=reason
            )

        caption_raw = payload.get("suggested_caption")
        caption: str | None = None
        if isinstance(caption_raw, str):
            stripped = caption_raw.strip()
            if stripped:
                caption = stripped[:_CAPTION_MAX_CHARS]

        material_id = self._repo.add(
            project_id=project_id,
            kind=kind_raw,
            local_path=view.local_path,
            byte_size=view.byte_size,
            caption=caption,
            tags=[],
            telegram_file_id=None,
            source_operator_file_id=operator_file_short_id,
            now=now,
        )
        logger.info(
            "sales_kb_material_registered",
            extra={
                "operator_file_short_id": operator_file_short_id,
                "project_id": project_id,
                "material_id": material_id,
                "kind": kind_raw,
            },
        )
        return AnalysisOutcome(
            registered=True, material_id=material_id, reason=reason
        )

"""``ServicesExtractor`` — LLM-driven services extractor from KB uploads (Story 12.05c).

After a successful KB ingest the bot_gateway calls
``POST /sales/services/extract-from-kb-file`` which dispatches into this
extractor. We read file metadata + extracted text via the existing
``operator_files_view`` (shared with the 12.05b materials analyzer),
clip the text to the first 6000 chars, and ask the LLM to enumerate
service / tour / activity offerings the document describes.

Confidential files short-circuit before the LLM call. Any LLM error or
schema violation resolves to ``ExtractionOutcome(added=[], reason=...)``
and is logged — never propagated. Idempotency on ``(project_id, name)``
is enforced via ``services_repo.find_by_name``: an existing service is
soft-skipped (no overwrite of an operator-crafted description), the
extractor only ``add``-s rows whose name does not already exist.

The extracted service names and descriptions MUST NEVER appear in our
own log lines — only counts are logged. Tests assert that invariant.
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

EXTRACTED_TEXT_CAP = 6000

_SYSTEM_PROMPT_PATH = (
    Path(__file__).parent / "system_prompts" / "operator_services_extractor.txt"
)


@dataclass(frozen=True)
class AddedService:
    service_id: int
    name: str


@dataclass(frozen=True)
class ExtractionOutcome:
    added: list[AddedService]
    skipped_existing: list[str]
    reason: str


class _OperatorFilesView(Protocol):
    def get_for_kb_material(
        self, *, short_id: str
    ) -> KbFileMaterialView | None: ...


class _OpenRouterClient(Protocol):
    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]: ...


class _ServicesRepo(Protocol):
    def find_by_name(
        self, *, project_id: int, name: str
    ) -> Any: ...

    def add(
        self,
        *,
        project_id: int,
        name: str,
        description_md: str | None,
        tags: list[str],
        now: datetime,
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


class ServicesExtractor:
    def __init__(
        self,
        *,
        openrouter: _OpenRouterClient,
        operator_files_view: _OperatorFilesView,
        services_repo: _ServicesRepo,
    ) -> None:
        self._openrouter = openrouter
        self._files = operator_files_view
        self._repo = services_repo

    async def extract_and_register(
        self,
        *,
        project_id: int,
        operator_file_short_id: str,
        now: datetime,
    ) -> ExtractionOutcome:
        view = self._files.get_for_kb_material(
            short_id=operator_file_short_id
        )
        if view is None:
            logger.info(
                "sales_services_skipped_missing_file",
                extra={
                    "operator_file_short_id": operator_file_short_id,
                    "project_id": project_id,
                },
            )
            return ExtractionOutcome(
                added=[], skipped_existing=[], reason="operator_file_not_found"
            )

        if view.is_confidential:
            logger.info(
                "sales_services_skipped_confidential",
                extra={
                    "operator_file_short_id": operator_file_short_id,
                    "project_id": project_id,
                },
            )
            return ExtractionOutcome(
                added=[], skipped_existing=[], reason="confidential_kb_file"
            )

        if not view.extracted_text or not view.extracted_text.strip():
            logger.info(
                "sales_services_skipped_empty_text",
                extra={
                    "operator_file_short_id": operator_file_short_id,
                    "project_id": project_id,
                },
            )
            return ExtractionOutcome(
                added=[], skipped_existing=[], reason="empty_extracted_text"
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
                "sales_services_schema_violation",
                extra={
                    "operator_file_short_id": operator_file_short_id,
                    "project_id": project_id,
                    "stage": "non_json_response",
                },
            )
            return ExtractionOutcome(
                added=[], skipped_existing=[], reason="llm_schema_violation"
            )
        except Exception as exc:
            logger.warning(
                "sales_services_extract_failed",
                extra={
                    "operator_file_short_id": operator_file_short_id,
                    "project_id": project_id,
                    "error": repr(exc),
                },
            )
            return ExtractionOutcome(
                added=[], skipped_existing=[], reason="llm_error"
            )

        services_raw = payload.get("services")
        if not isinstance(services_raw, list):
            logger.warning(
                "sales_services_schema_violation",
                extra={
                    "operator_file_short_id": operator_file_short_id,
                    "project_id": project_id,
                    "stage": "services_field_invalid",
                },
            )
            return ExtractionOutcome(
                added=[], skipped_existing=[], reason="llm_schema_violation"
            )

        reason_raw = payload.get("reason")
        reason = (
            reason_raw.strip()
            if isinstance(reason_raw, str) and reason_raw.strip()
            else "ok"
        )

        added: list[AddedService] = []
        skipped: list[str] = []
        for entry in services_raw:
            if not isinstance(entry, dict):
                continue
            name_raw = entry.get("name")
            if not isinstance(name_raw, str):
                continue
            name = name_raw.strip()
            if not name:
                continue
            description_raw = entry.get("description")
            description: str | None
            if isinstance(description_raw, str):
                stripped = description_raw.strip()
                description = stripped if stripped else None
            else:
                description = None

            existing = self._repo.find_by_name(
                project_id=project_id, name=name
            )
            if existing is not None:
                skipped.append(name)
                continue

            service_id = self._repo.add(
                project_id=project_id,
                name=name,
                description_md=description,
                tags=[],
                now=now,
            )
            added.append(AddedService(service_id=service_id, name=name))

        logger.info(
            "sales_services_extracted_count",
            extra={
                "operator_file_short_id": operator_file_short_id,
                "project_id": project_id,
                "count_added": len(added),
                "count_skipped": len(skipped),
            },
        )
        return ExtractionOutcome(
            added=added, skipped_existing=skipped, reason=reason
        )

"""``ClientMaterialsSelector`` — pick the best client material for a moment.

Story 12.05 — the autonomous dispatcher always calls
``selector.pick(*, project_id, intent_tags, purpose=...)`` so the lookup
always includes the structural ``purpose`` tag (``tour_preview``,
``equipment_gallery``, ``catalog``). The selector calls
``repo.pick_by_tags(... tags=intent_tags + [purpose])`` and returns the
top-1 row by overlap count, with ties broken by ``updated_at`` desc so a
freshly registered material wins over a stale duplicate.

Returns ``None`` when the repo finds no overlap — the answerer treats that
as a no-op for the media moment and never fabricates a "вот видео" reply
without dispatching one.
"""

from __future__ import annotations

from typing import Literal, Protocol

from services.api.app.sales.client_materials_repository import ClientMaterial

MaterialPurpose = Literal["tour_preview", "equipment_gallery", "catalog"]


class _MaterialsRepo(Protocol):
    def pick_by_tags(
        self, *, project_id: int, tags: list[str]
    ) -> list[ClientMaterial]: ...


class ClientMaterialsSelector:
    def __init__(self, *, repo: _MaterialsRepo) -> None:
        self._repo = repo

    def pick(
        self,
        *,
        project_id: int,
        intent_tags: list[str],
        purpose: MaterialPurpose,
    ) -> ClientMaterial | None:
        lookup_tags = list(intent_tags) + [purpose]
        candidates = self._repo.pick_by_tags(
            project_id=project_id, tags=lookup_tags
        )
        if not candidates:
            return None
        requested = set(lookup_tags)
        ranked = sorted(
            candidates,
            key=lambda row: (
                -len(requested.intersection(row.tags)),
                _updated_at_sort_key(row.updated_at),
            ),
        )
        return ranked[0]


def _updated_at_sort_key(updated_at: str) -> str:
    """Inverse-string sort so most-recent ISO timestamp ranks first.

    ISO-8601 strings sort lexicographically in chronological order, so
    inverting the per-character order would be wrong. Instead we negate by
    prefixing a sentinel that flips the comparison — `chr(0xFFFF)` minus
    each codepoint produces a stable ``updated_at`` desc tiebreaker without
    parsing the timestamp.
    """
    return "".join(chr(0xFFFF - ord(ch)) for ch in updated_at)


__all__ = ["ClientMaterialsSelector", "MaterialPurpose"]

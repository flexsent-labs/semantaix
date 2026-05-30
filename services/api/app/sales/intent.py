"""Typed sales `Intent` + merge helper (Epic 12, Story 12.03 / 12.15).

`Intent` collects the fields a sales conversation establishes during scoping.
The five transfer fields (dates, headcount, vehicle_count, difficulty, drivers)
are first-class attributes for back-compat and for the consumers that read them
by name (calendar reads ``dates``; the material selector reads ``difficulty``).
A per-service anketa (Story 12.15) may collect OTHER keys — those live in the
``extra`` map and round-trip through state alongside the legacy five.

`intent_merge` is the only helper that folds the LLM's ``extracted_fields`` dict
into an `Intent`: absent / ``None`` values are ignored (never clobber a populated
field), and — when an ``allowed`` key set is given — only those keys are taken,
so a custom schema admits its own fields without letting stray keys leak in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

_FIELD_NAMES: tuple[str, ...] = (
    "dates",
    "headcount",
    "vehicle_count",
    "difficulty",
    "drivers",
)


@dataclass(frozen=True)
class Intent:
    """Collected scoping fields — the five typed transfer fields plus ``extra``.

    Each value is ``str | int | None`` per the schema in story 12.01. The
    answerer accepts whichever shape the LLM produced (Russian free-form
    strings for dates / drivers; ints for headcount / vehicle_count;
    short tags for difficulty). Custom per-service fields land in ``extra``.
    """

    dates: str | int | None = None
    headcount: str | int | None = None
    vehicle_count: str | int | None = None
    difficulty: str | int | None = None
    drivers: str | int | None = None
    # Story 12.15 — arbitrary per-service fields, keyed by their schema key.
    # Excluded from __hash__ (a dict is unhashable) but kept in equality so
    # state round-trips compare correctly.
    extra: dict[str, str | int | None] = field(default_factory=dict, hash=False)

    def get(self, name: str) -> str | int | None:
        """Value for any field — legacy attribute or custom ``extra`` key."""
        if name in _FIELD_NAMES:
            return getattr(self, name)
        return self.extra.get(name)

    def with_field(self, name: str, value: str | int | None) -> Intent:
        """Return a new Intent with one field set (legacy or custom)."""
        if name in _FIELD_NAMES:
            return replace(self, **{name: value})
        return replace(self, extra={**self.extra, name: value})

    def missing_fields(
        self, required: Iterable[str] | None = None
    ) -> list[str]:
        """Required field names whose value is still ``None``, in order.

        ``required`` (Story 12.12 / 12.15) scopes completeness to a configured
        field set (the active anketa). ``None`` keeps the legacy behaviour —
        all five transfer fields.
        """
        names = tuple(required) if required is not None else _FIELD_NAMES
        return [name for name in names if self.get(name) is None]

    def is_complete(self, required: Iterable[str] | None = None) -> bool:
        return not self.missing_fields(required)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            name: getattr(self, name) for name in _FIELD_NAMES
        }
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Intent:
        """Build an Intent from a dict (e.g. JSON-decoded state row).

        Canonical five keys become attributes; every other key is retained in
        ``extra`` (Story 12.15 — custom per-service fields survive a round-trip).
        """
        legacy = {
            name: payload.get(name)
            for name in _FIELD_NAMES
            if name in payload
        }
        extra = {
            key: value
            for key, value in payload.items()
            if key not in _FIELD_NAMES
        }
        return cls(**legacy, extra=extra)


def intent_merge(
    existing: Intent,
    extracted: Mapping[str, Any],
    *,
    allowed: Iterable[str] | None = None,
) -> Intent:
    """Return a new Intent with extracted fields merged in.

    Rules:
      * Only keys in ``allowed`` are considered; ``None`` defaults to the
        canonical five (back-compat — stray keys are ignored).
      * ``None`` values are ignored (never overwrite a populated field).
      * Explicit non-None values replace the existing field.
    """
    keys = tuple(allowed) if allowed is not None else _FIELD_NAMES
    result = existing
    for name in keys:
        if name not in extracted:
            continue
        value = extracted[name]
        if value is None:
            continue
        result = result.with_field(name, value)
    return result

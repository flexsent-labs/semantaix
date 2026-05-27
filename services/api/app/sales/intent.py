"""Typed sales `Intent` + merge helper (Epic 12, Story 12.03).

`Intent` is a frozen dataclass with exactly the five fields the Дарья
dialog establishes during scoping. `intent_merge` is the only helper that
mutates an `Intent` shape — callers feed it the LLM's `extracted_fields`
dict and get back a new `Intent` with absent / `None` values ignored, so
the merge never propagates `None` over a populated field.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
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
    """Five typed fields collected during scoping.

    Each value is ``str | int | None`` per the schema in story 12.01. The
    answerer accepts whichever shape the LLM produced (Russian free-form
    strings for dates / drivers; ints for headcount / vehicle_count;
    short tags for difficulty).
    """

    dates: str | int | None = None
    headcount: str | int | None = None
    vehicle_count: str | int | None = None
    difficulty: str | int | None = None
    drivers: str | int | None = None

    def missing_fields(self) -> list[str]:
        """Field names whose value is still ``None``, in canonical order."""
        return [name for name in _FIELD_NAMES if getattr(self, name) is None]

    def is_complete(self) -> bool:
        return not self.missing_fields()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Intent:
        """Build an Intent from a dict (e.g. JSON-decoded state row).

        Unknown keys are silently ignored — the wire format only carries
        the canonical five fields, but tests and migrations may include
        extras.
        """
        kwargs = {
            name: payload.get(name)
            for name in _FIELD_NAMES
            if name in payload
        }
        return cls(**kwargs)


def intent_merge(existing: Intent, extracted: dict[str, Any]) -> Intent:
    """Return a new Intent with extracted fields merged in.

    Rules:
      * Unknown keys are ignored.
      * `None` values are ignored (never overwrite a populated field).
      * Explicit non-None values replace the existing field.
    """
    updates: dict[str, Any] = {}
    for name in _FIELD_NAMES:
        if name not in extracted:
            continue
        value = extracted[name]
        if value is None:
            continue
        updates[name] = value
    if not updates:
        return existing
    return replace(existing, **updates)

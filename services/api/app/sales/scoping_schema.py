"""Per-service scoping schema (Epic 12, Story 12.15).

A `ScopingSchema` is the anketa a service collects: an ordered list of
`ScopingField`s, each with a customer-facing question, a kind (``text`` or
``number`` — the latter is eligible for deterministic numeric capture), and a
``required`` flag. The scoping prompt, fallback questions, completeness check
and the ``intent_merge`` allow-list are all derived from the active schema, so a
new domain is a data change, not a code change.

Two built-ins ship as presets:

* ``TRANSFER_SCHEMA`` — the legacy five transfer fields (behaviour-preserving;
  ``dates`` feeds the calendar, ``difficulty`` feeds material tags).
* ``CONSULTATION_SCHEMA`` — a call booking: date/time + topic + contact, with no
  headcount/vehicle questions (the "1 person" case).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScopingField:
    key: str
    question: str
    kind: str = "text"  # "text" | "number"
    required: bool = True


@dataclass(frozen=True)
class ScopingSchema:
    fields: tuple[ScopingField, ...]

    def keys(self) -> tuple[str, ...]:
        return tuple(field.key for field in self.fields)

    def required_keys(self) -> tuple[str, ...]:
        return tuple(field.key for field in self.fields if field.required)

    def numeric_keys(self) -> frozenset[str]:
        return frozenset(
            field.key for field in self.fields if field.kind == "number"
        )

    def question_for(self, key: str) -> str | None:
        for field in self.fields:
            if field.key == key:
                return field.question
        return None

    def with_required(self, required: tuple[str, ...]) -> ScopingSchema:
        """Same field list, with ``required`` flipped to the given key set.

        Lets the legacy ``scoping_required_fields`` config narrow the built-in
        transfer schema without redefining its questions (Story 12.12 bridge).
        """
        wanted = set(required)
        return ScopingSchema(
            tuple(
                ScopingField(
                    key=field.key,
                    question=field.question,
                    kind=field.kind,
                    required=field.key in wanted,
                )
                for field in self.fields
            )
        )


TRANSFER_SCHEMA = ScopingSchema(
    (
        ScopingField("dates", "На какую дату планируете?", kind="text"),
        ScopingField("headcount", "Сколько человек поедет?", kind="number"),
        ScopingField("vehicle_count", "Сколько багги нужно?", kind="number"),
        ScopingField(
            "difficulty", "Какой сложности маршрут предпочитаете?", kind="text"
        ),
        ScopingField("drivers", "Сколько нужно водителей?", kind="number"),
    )
)


CONSULTATION_SCHEMA = ScopingSchema(
    (
        ScopingField(
            "dates", "На какую дату и время вам удобно созвониться?", kind="text"
        ),
        ScopingField("topic", "Какой вопрос хотите обсудить?", kind="text"),
        ScopingField(
            "contact", "Оставьте, пожалуйста, телефон для связи.", kind="text"
        ),
    )
)


# Preset registry for the operator command (Story 12.18).
SCHEMA_PRESETS: dict[str, ScopingSchema] = {
    "transfer": TRANSFER_SCHEMA,
    "consultation": CONSULTATION_SCHEMA,
}

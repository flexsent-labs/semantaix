"""Sales-persona answerer package (Epic 12).

The `SalesPersonaAnswerer` drives the multi-turn sales conversation
described in `_bmad-output/planning-artifacts/epics/stories/epic-12/`.
Story 12.03 ships the greeting + intent-scoping stages in isolation.
"""

from services.api.app.sales.intent import Intent, intent_merge
from services.api.app.sales.sales_persona_answerer import (
    LlmSchemaViolation,
    SalesPersonaAnswerer,
)
from services.api.app.sales.state_repository import StateRepository

__all__ = [
    "Intent",
    "LlmSchemaViolation",
    "SalesPersonaAnswerer",
    "StateRepository",
    "intent_merge",
]

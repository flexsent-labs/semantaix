"""Story 12.35 (D8) — the sales LLM lines mirror the customer's language.

The greeting/scoping/proposal/pricing/concept/catalog prompts hardcoded
Russian output ("…на русском"); an English customer got a Russian reply. The
fix instructs every sales LLM prompt to reply in the SAME language the customer
used (default Russian). Prompt-text only — no funnel-logic change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    TRANSFER_SCHEMA,
    _build_greeting_prompt,
    _build_scoping_prompt,
)

_TODAY = "1 июня 2026 года, понедельник"
_PROMPTS = Path("services/api/app/sales/system_prompts")
_MIRROR = "на том же языке"  # the language-mirroring directive


def test_greeting_prompt_mirrors_customer_language() -> None:
    prompt = _build_greeting_prompt(today=_TODAY)
    assert _MIRROR in prompt
    assert "на русском" not in prompt  # no longer pins Russian-only output


def test_scoping_prompt_mirrors_customer_language() -> None:
    prompt = _build_scoping_prompt(
        persona="Анна", intent=Intent(), schema=TRANSFER_SCHEMA, today=_TODAY
    )
    assert _MIRROR in prompt
    assert "на русском" not in prompt


# Scoped to the funnel-question prompts (greeting + scoping). The proposal,
# pricing, and concept prompts carry verbatim-token / grounding verifiers that
# expect Russian output — mirroring those needs verifier-aware localization and
# is a documented follow-up (see the story doc).
@pytest.mark.parametrize("name", ["sales_greeting.txt", "sales_scoping.txt"])
def test_funnel_prompts_instruct_language_mirroring(name: str) -> None:
    text = (_PROMPTS / name).read_text(encoding="utf-8")
    assert _MIRROR in text, f"{name} missing the language-mirroring instruction"
    assert "на русском" not in text, f"{name} still pins Russian-only output"

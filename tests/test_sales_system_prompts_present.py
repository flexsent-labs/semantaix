"""Story 12.09 — persona prompt-file presence.

Every persona system prompt the answerer / fire handler / materials
analyzer expects on disk must exist and be non-empty. Each file is
checked in so an operator can tweak persona copy without a redeploy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PROMPTS_DIR = (
    Path(__file__).resolve().parent.parent
    / "services"
    / "api"
    / "app"
    / "sales"
    / "system_prompts"
)

_EXPECTED_FILES: tuple[str, ...] = (
    "sales_greeting.txt",
    "sales_scoping.txt",
    "sales_pricing_hit.txt",
    "sales_proposal.txt",
    "sales_followup.txt",
    "sales_catalog.txt",
    "sales_concept_rag.txt",
    "sales_kb_material_analyzer.txt",
)


@pytest.mark.parametrize("name", _EXPECTED_FILES)
def test_persona_prompt_file_exists_and_non_empty(name: str) -> None:
    path = _PROMPTS_DIR / name
    assert path.is_file(), f"missing prompt file: {path}"
    contents = path.read_text(encoding="utf-8").strip()
    assert contents, f"prompt file is empty: {path}"

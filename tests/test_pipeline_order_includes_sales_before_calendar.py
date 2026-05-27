"""Story 12.09 — pipeline-order regression.

The live ``AnswerPipeline`` must include ``sales_persona`` immediately
before ``calendar_availability``. Pipeline order IS the routing logic:
the sales answerer owns greeting/scoping/pricing/proposing turns and
must short-circuit before the calendar answerer ever runs.
"""

from __future__ import annotations


def test_main_pipeline_places_sales_persona_immediately_before_calendar() -> None:
    from services.api.app import main as api_main

    names = [a.name for a in api_main.answer_pipeline.answerers]
    assert "sales_persona" in names, names
    assert "calendar_availability" in names, names
    sales_idx = names.index("sales_persona")
    calendar_idx = names.index("calendar_availability")
    assert sales_idx + 1 == calendar_idx, names


def test_main_pipeline_places_sales_persona_before_grounded_rag() -> None:
    from services.api.app import main as api_main

    names = [a.name for a in api_main.answer_pipeline.answerers]
    assert names.index("sales_persona") < names.index("grounded_rag")

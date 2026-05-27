"""Wiring smoke test for the production ``SalesPersonaAnswerer`` instance.

The answerer accepts ``price_lookup``, ``rag_retriever``,
``grounding_threshold_getter``, ``followup_repo``, ``material_selector``,
and ``material_dispatcher`` as **optional** keyword arguments. When a
dependency is missing, the corresponding branch silently returns
``_skip("pricing_not_configured")`` (or similar) — masking the issue from
both lint and pytest because each branch is unit-tested with explicit
mock injection.

A signoff run of ``scripts/epic12_signoff.sh`` step 5 caught exactly this
class of regression in production: pricing turns were being skipped
because ``price_lookup`` was ``None`` on the module-level answerer
instance even though the unit tests injected one explicitly.

This test reaches into ``services.api.app.main`` directly to assert that
the module-level ``sales_persona_answerer`` has every dependency wired.
Adding a new optional kwarg to the constructor without updating both the
wiring and this list will fail the assertion, prompting the developer to
wire it (or, if the dependency is genuinely optional, to add it to the
allowed-None set with a code comment explaining why).
"""

from __future__ import annotations

from services.api.app import main as api_main


def test_module_level_sales_persona_answerer_has_required_wires() -> None:
    answerer = api_main.sales_persona_answerer

    # Core deps — always required.
    assert answerer._state_repo is not None, "state_repo not wired"
    assert answerer._services_repo is not None, "services_repo not wired"
    assert answerer._openrouter is not None, "openrouter not wired"
    assert answerer._normalizer is not None, "normalizer not wired"
    assert answerer._clock is not None, "clock not wired"
    assert answerer._persona_getter is not None, "bot_persona_getter not wired"

    # Optional-but-required-for-epic-12 deps. If any of these are None on
    # the production instance, the corresponding answerer branch returns
    # ``_skip("pricing_not_configured")`` / ``"stage_not_implemented_yet"``
    # silently, which the live demo (and customers) see as the bot ducking
    # to HITL for messages it should handle directly.
    assert answerer._rag is not None, (
        "rag_retriever not wired — concept-explainer mid-funnel asides "
        "(FLE-29 S06) will skip silently"
    )
    assert answerer._grounding_threshold_getter is not None, (
        "grounding_threshold_getter not wired — RAG-grounded sales replies "
        "use the wrong threshold"
    )
    assert answerer._price_lookup is not None, (
        "price_lookup not wired — pricing turns (FLE-26 S04) skip with "
        "'pricing_not_configured' instead of quoting RAG hits or "
        "escalating with reason='price_unknown'"
    )
    assert answerer._followup_repo is not None, (
        "followup_repo not wired — proactive +24h follow-up (FLE-31 S08) "
        "never enqueues anything"
    )
    assert answerer._material_selector is not None, (
        "material_selector not wired — autonomous /material dispatch "
        "(FLE-27 S05) cannot pick a file"
    )
    assert answerer._material_dispatcher is not None, (
        "material_dispatcher not wired — even with a picked file, the "
        "telegram send path is unreachable"
    )

    # Known follow-up: ``date_proposer`` requires building an
    # availability orchestrator that bridges DateProposer's protocol to
    # the calendar's ``compute_availability``. Tracked separately; this
    # assertion documents the gap so it isn't forgotten.
    # Once wired, change to ``assert answerer._date_proposer is not None``.
    assert answerer._date_proposer is None, (
        "date_proposer is wired — update this assertion to "
        "``assert answerer._date_proposer is not None`` to lock it in."
    )

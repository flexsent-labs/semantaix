"""LLM model-availability guard (Story 12.41, round-7 investigation).

OpenRouter retires model slugs over time. A retired slug returns 404 on
``/chat/completions``, which makes every persona/grounding call fail and silently
degrades the bot (greeting → "Это не ко мне", bookings escalate/stall) — it cost
multiple live-QA rounds to trace. This validates the configured models against
OpenRouter's live model list so a deprecation is caught loudly (startup log +
``/health/model``) instead of via customer-facing breakage.

Conservative: an unconfigured key (unit tests) or an unreachable model list is
treated as "can't verify", NOT "unavailable" — it never false-alarms.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class _ModelClient(Protocol):
    def _is_configured(self) -> bool: ...

    async def fetch_available_model_ids(self) -> set[str]: ...


async def find_unavailable_models(
    *, client: _ModelClient, models: list[str | None]
) -> list[str]:
    """Return the configured ``models`` that OpenRouter no longer serves.

    Empty list = all good, OR the check couldn't run (no API key / model list
    unreachable). Logs ``llm_model_unavailable`` (ERROR) per missing model and
    ``llm_model_check_failed`` (WARNING) when the list can't be fetched.
    """
    configured = sorted({m for m in models if m})
    if not configured or not client._is_configured():
        return []
    try:
        available = await client.fetch_available_model_ids()
    except Exception as exc:  # any transport/parse error: can't verify, don't alarm
        logger.warning(
            "llm_model_check_failed", extra={"error": str(exc) or repr(exc)}
        )
        return []
    missing = [model for model in configured if model not in available]
    for model in missing:
        logger.error("llm_model_unavailable", extra={"model": model})
    return missing


__all__ = ["find_unavailable_models"]

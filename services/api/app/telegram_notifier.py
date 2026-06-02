from __future__ import annotations

import httpx

CRITICAL_FINGERPRINTS = {
    "provider429_spike",
    "provider5xx_spike",
    "vectordb_down",
    "db_down",
    "queue_dlq_growth",
    "hitl_delivery_failures",
    "knowledge_extraction_failures",
    "knowledge_reindex_failures",
    # Story 12.42 — a configured LLM model OpenRouter no longer serves: every
    # persona call 404s and the bot silently degrades, so DM the admin.
    "llm_model_unavailable",
}


class TelegramIncidentNotifier:
    def __init__(
        self,
        *,
        bot_token: str,
        alert_chat_id: str | None,
        alert_username: str,
        base_url: str = "https://api.telegram.org",
    ) -> None:
        self.bot_token = bot_token
        self.alert_chat_id = alert_chat_id
        self.alert_username = alert_username
        self._base_url = base_url.rstrip("/")

    async def notify_if_critical(
        self,
        *,
        incident_id: int,
        fingerprint: str,
        severity: str,
        summary: str,
        occurrence_count: int,
    ) -> tuple[bool, str]:
        if not self.is_critical_event(fingerprint=fingerprint, severity=severity):
            return False, "not_critical"

        if self.bot_token == "replace-me" or not self.bot_token:
            return False, "missing_bot_token"
        if not self.alert_chat_id:
            return False, "missing_alert_chat_id"

        text = (
            f"Critical incident for {self.alert_username}\n"
            f"- incident_id: {incident_id}\n"
            f"- fingerprint: {fingerprint}\n"
            f"- summary: {summary}\n"
            f"- occurrences: {occurrence_count}"
        )

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self._base_url}/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.alert_chat_id, "text": text},
            )
            response.raise_for_status()
        return True, "sent"

    @staticmethod
    def is_critical_event(*, fingerprint: str, severity: str) -> bool:
        return severity.lower() == "critical" and fingerprint in CRITICAL_FINGERPRINTS

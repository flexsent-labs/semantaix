from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from platform_common.settings import get_settings
from services.api.app.rag import RagChunk


class OpenRouterJsonSchemaViolation(Exception):
    """Raised by `OpenRouterClient.complete_json` when the response is not
    valid JSON. Callers convert this into their own domain-specific
    schema-violation type (e.g. ``LlmSchemaViolation`` in the sales
    answerer) so the pipeline can fall through cleanly.
    """

_GROUNDING_SYSTEM_PROMPT_TEMPLATE = (
    "Ты — {name}, сотрудник компании, описанной в приведённых ниже "
    "фрагментах знаний. Эта компания — твой работодатель: говори о её "
    "услугах, маршрутах, ценах и порядке работы от первого лица "
    "множественного числа («у нас», «мы предлагаем», «наша компания», "
    "«наши маршруты»). Никогда не называй компанию в третьем лице "
    "(«Компания X предлагает…», «ИП … предлагает…» — запрещено) и не "
    "цитируй её юридическое название или организационно-правовую форму. "
    "Никогда не пиши, что ты бот, ассистент, AI, ИИ, нейросеть или "
    "языковая модель. Если спросят, отвечай как обычный человек на работе. "
    "Отвечай на том же языке, что и вопрос пользователя; если вопрос "
    "на русском — отвечай формальным русским, без сленга и нецензурной "
    "лексики. Используй ТОЛЬКО приведённые ниже фрагменты знаний, "
    "сегодняшнюю дату ({today_iso}) и предоставленный справочный контекст "
    "для планирования (если он есть). Сегодняшняя дата дана только как "
    "справка: не указывай и не упоминай её в ответе, если пользователь явно "
    "не спрашивает о дате, времени, дне недели или расписании. Если ответ "
    "не полностью подтверждён "
    "этими источниками — верни строго `ESCALATE_TO_HUMAN` без каких-либо "
    "других слов. Не хеджируй (\"возможно\", \"вроде\", \"наверное\", "
    "\"хз\") — либо точный ответ, либо `ESCALATE_TO_HUMAN`. "
    # Story 13.06 (FR-25): humanistic, question-tailored catalog answers.
    "Если клиент просто спрашивает, какие есть услуги — перечисли только "
    "названия, естественно и кратко. Если клиент спрашивает про цену, "
    "детали, описание или конкретную услугу — добавь только то, что он "
    "спросил. Не дампи всё подряд."
)

_CATALOG_DIGEST_SYSTEM_PROMPT = (
    "Тебе дан фрагмент базы знаний компании (одно или несколько утверждений). "
    "Составь краткий структурированный список РАЗНЫХ услуг, товаров или "
    "предложений компании, которые в нём упомянуты. Каждый пункт — с новой "
    "строки, начинай с «- », одна услуга на строку, без цен и лишних "
    "подробностей, только название/суть предложения. Объединяй дубликаты и "
    "близкие формулировки в один пункт. Не выдумывай того, чего нет в тексте. "
    "Если в тексте нет ни одной услуги, товара или предложения — верни строго "
    "`NO_OFFERINGS` без других слов."
)

_VERIFIER_SYSTEM_PROMPT = (
    "Given the question, the candidate answer, and the snippets, decide "
    "whether the answer is fully supported by the snippets (today's date "
    "and any provided scheduling context count as supporting facts). Reply "
    "with exactly `GROUNDED: <one-sentence reason>` or "
    "`NOT_GROUNDED: <one-sentence reason>`. The candidate answer may be "
    "in Russian or English; the verdict reason should be in English for "
    "log readability."
)


@dataclass(frozen=True)
class GroundingVerdict:
    label: str  # "GROUNDED" or "NOT_GROUNDED"
    reason: str


def _format_snippets(snippets: list[RagChunk]) -> str:
    return "\n".join(f"- [{chunk.source_id}] {chunk.chunk_text}" for chunk in snippets)


def _maybe_scheduling_block(scheduling_context: str | None) -> list[str]:
    if not scheduling_context:
        return []
    return [scheduling_context]


def _build_grounding_system_prompt(
    *,
    first_name: str,
    last_name: str,
    today_iso: str,
    template: str | None = None,
) -> str:
    name = f"{first_name} {last_name}".strip()
    chosen = template if template is not None else _GROUNDING_SYSTEM_PROMPT_TEMPLATE
    return chosen.format(name=name, today_iso=today_iso)


class OpenRouterClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.openrouter_api_key
        self.base_url = settings.openrouter_base_url.rstrip("/")
        self.grounding_model = settings.openrouter_grounding_model

    async def _chat(
        self, *, model: str, messages: list[dict[str, Any]]
    ) -> str:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        payload = {"model": model, "messages": messages}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]

    async def answer_grounded(
        self,
        *,
        question: str,
        snippets: list[RagChunk],
        today_iso: str,
        persona_first_name: str,
        persona_last_name: str,
        model: str | None = None,
        system_prompt_template: str | None = None,
        scheduling_context: str | None = None,
    ) -> str:
        system = _build_grounding_system_prompt(
            first_name=persona_first_name,
            last_name=persona_last_name,
            today_iso=today_iso,
            template=system_prompt_template,
        )
        user_block = "\n\n".join(
            [
                "Snippets:\n" + _format_snippets(snippets),
                *_maybe_scheduling_block(scheduling_context),
                "Question:\n" + question,
            ]
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_block},
        ]
        return await self._chat(
            model=model or self.grounding_model, messages=messages
        )

    async def summarize_offerings(
        self,
        *,
        knowledge_text: str,
        model: str | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """Extract a deduplicated offerings list from a block of knowledge text.

        Used for one map or reduce step of the catalog-digest build; the
        map-reduce loop itself lives in ``CatalogDigestService``.
        """
        chosen_system = (
            system_prompt
            if system_prompt is not None
            else _CATALOG_DIGEST_SYSTEM_PROMPT
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": chosen_system},
            {"role": "user", "content": knowledge_text},
        ]
        return await self._chat(
            model=model or self.grounding_model, messages=messages
        )

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Single structured JSON-out completion.

        Used by the SalesPersonaAnswerer (Story 12.03) for per-turn
        extraction + next-question generation. The response is expected
        to be a JSON object; we ask OpenRouter to enforce that via the
        ``response_format=json_object`` payload field. On parse failure
        we raise ``OpenRouterJsonSchemaViolation`` so the caller can
        log + fall through without raising into the pipeline.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        payload: dict[str, Any] = {
            "model": model or self.grounding_model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            raw = data["choices"][0]["message"]["content"]
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OpenRouterJsonSchemaViolation(
                f"non-JSON response: {raw[:200]}"
            ) from exc
        if not isinstance(decoded, dict):
            raise OpenRouterJsonSchemaViolation(
                f"JSON response is not an object: {type(decoded).__name__}"
            )
        return decoded

    async def verify_grounding(
        self,
        *,
        question: str,
        answer: str,
        snippets: list[RagChunk],
        model: str | None = None,
        system_prompt: str | None = None,
        scheduling_context: str | None = None,
    ) -> GroundingVerdict:
        user_block = "\n\n".join(
            [
                "Snippets:\n" + _format_snippets(snippets),
                *_maybe_scheduling_block(scheduling_context),
                "Question:\n" + question,
                "Candidate answer:\n" + answer,
            ]
        )
        chosen_system = (
            system_prompt if system_prompt is not None else _VERIFIER_SYSTEM_PROMPT
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": chosen_system},
            {"role": "user", "content": user_block},
        ]
        raw = await self._chat(
            model=model or self.grounding_model, messages=messages
        )
        return _parse_verdict(raw)


def _parse_verdict(raw: str) -> GroundingVerdict:
    text = raw.strip()
    upper = text.upper()
    if upper.startswith("GROUNDED"):
        reason = text.split(":", 1)[1].strip() if ":" in text else ""
        return GroundingVerdict(label="GROUNDED", reason=reason)
    if upper.startswith("NOT_GROUNDED"):
        reason = text.split(":", 1)[1].strip() if ":" in text else ""
        return GroundingVerdict(label="NOT_GROUNDED", reason=reason)
    # Defensive: treat unparseable verifier output as NOT_GROUNDED so we
    # escalate to HITL rather than risk a hallucinated answer.
    return GroundingVerdict(
        label="NOT_GROUNDED", reason=f"unparseable verifier output: {text[:60]}"
    )

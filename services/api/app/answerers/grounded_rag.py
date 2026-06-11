from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from services.api.app.answerers import AnswerContext, AnswerResult
from services.api.app.answerers.catalog_merge import merge_structured_with_digest
from services.api.app.answerers.scheduling_context import build_scheduling_context
from services.api.app.answerers.service_catalog_intent import (
    is_service_catalog_query,
)
from services.api.app.answerers.weather_client import WeatherClient
from services.api.app.calendar.project_services_repository import ProjectService
from services.api.app.guardrails import evaluate_suggestion
from services.api.app.openrouter_client import LlmUsageCapture, OpenRouterClient
from services.api.app.project_prompts import (
    ProjectPromptRepository,
    resolve_prompt,
    split_guardrail_lines,
)
from services.api.app.rag import RagChunk
from services.api.app.russian_text import get_russian_normalizer

if TYPE_CHECKING:
    from services.api.app.usage.recorder import UsageRecorder

_SENTINEL = "ESCALATE_TO_HUMAN"
_ANSWER_SNIPPET_MAX = 200

logger = logging.getLogger(__name__)


class _RagReader(Protocol):
    def retrieve(
        self,
        *,
        query: str,
        limit: int = 3,
        project_id: int | None = None,
    ) -> list[RagChunk]: ...


class _CatalogDigestProvider(Protocol):
    async def get_digest(self, *, project_id: int | None) -> str: ...


class _PersonaReader(Protocol):
    def __call__(self) -> tuple[str, str]: ...


class _ProjectServicesReader(Protocol):
    def list_for_project(self, *, project_id: int) -> list[ProjectService]: ...


class GroundedRagAnswerer:
    name = "grounded_rag"

    def __init__(
        self,
        *,
        rag_repository: _RagReader,
        openrouter_client: OpenRouterClient,
        persona_reader: _PersonaReader,
        project_prompt_repository: ProjectPromptRepository,
        catalog_digest_service: _CatalogDigestProvider,
        weather_client: WeatherClient | None = None,
        project_services_reader: _ProjectServicesReader | None = None,
        recorder: "UsageRecorder | None" = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._rag = rag_repository
        self._llm = openrouter_client
        self._persona_reader = persona_reader
        self._prompts = project_prompt_repository
        self._catalog_digest = catalog_digest_service
        self._weather_client = weather_client
        self._project_services_reader = project_services_reader
        self._recorder = recorder
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(timezone.utc))

    async def try_answer(
        self, *, question: str, ctx: AnswerContext
    ) -> AnswerResult:
        normalizer = get_russian_normalizer()
        catalog_query = is_service_catalog_query(
            text=question, normalizer=normalizer
        )
        if catalog_query:
            # Aggregate questions ("что ещё есть?") need the whole offerings set,
            # not a few lemma-overlapping lines. Story 13.06 (FR-25): read the
            # structured ``project_services`` rows first (humanistic prose, no
            # field labels), then merge with the LLM-built digest, deduping
            # digest sentences that the structured rows already cover.
            structured_rows: list[ProjectService] = []
            if (
                self._project_services_reader is not None
                and ctx.project_id is not None
            ):
                structured_rows = await asyncio.to_thread(
                    self._project_services_reader.list_for_project,
                    project_id=ctx.project_id,
                )
            digest = await self._catalog_digest.get_digest(
                project_id=ctx.project_id
            )
            merged_chunk, source_id_suffix = merge_structured_with_digest(
                structured_rows=structured_rows,
                digest_text=digest,
                normalizer=normalizer,
                trace_id=ctx.trace_id,
                project_id=ctx.project_id,
            )
            if source_id_suffix == "empty" or not merged_chunk.strip():
                return self._skip(
                    reason="catalog_empty",
                    ctx=ctx,
                    question=question,
                    chunks=[],
                )
            chunks = [
                RagChunk(
                    id=0,
                    source_id=f"{source_id_suffix}:{ctx.project_id}",
                    chunk_text=merged_chunk,
                    score=1.0,
                )
            ]
        else:
            chunks = self._rag.retrieve(
                query=question, limit=3, project_id=ctx.project_id
            )
            if not chunks:
                return self._skip(
                    reason="no_chunks",
                    ctx=ctx,
                    question=question,
                    chunks=chunks,
                )
            if chunks[0].score < ctx.grounding_threshold:
                return self._skip(
                    reason="below_threshold",
                    ctx=ctx,
                    question=question,
                    chunks=chunks,
                )

        logger.info(
            "grounded_rag_pipeline_entry",
            extra={
                "trace_id": ctx.trace_id,
                "top_score": chunks[0].score,
                "threshold": ctx.grounding_threshold,
                "chunk_source_ids": [c.source_id for c in chunks],
                "chunk_confidential_flags": [c.is_confidential for c in chunks],
                "chunk_project_ids": [c.project_id for c in chunks],
            },
        )

        today_iso = ctx.now.date().isoformat()
        first_name, last_name = self._persona_reader()
        grounding_template = resolve_prompt(
            self._prompts, ctx.project_id, "grounding_system"
        )
        verifier_prompt = resolve_prompt(
            self._prompts, ctx.project_id, "verifier_system"
        )
        hedge_lines = split_guardrail_lines(
            resolve_prompt(self._prompts, ctx.project_id, "guardrail_hedges")
        )
        policy_lines = split_guardrail_lines(
            resolve_prompt(self._prompts, ctx.project_id, "guardrail_policy")
        )
        profanity_lines = split_guardrail_lines(
            resolve_prompt(self._prompts, ctx.project_id, "guardrail_profanity")
        )
        scheduling_context = await build_scheduling_context(
            question=question, ctx=ctx, weather_client=self._weather_client
        )
        logger.info(
            "grounded_rag_llm_request",
            extra={
                "trace_id": ctx.trace_id,
                "persona_first_name": first_name,
                "persona_last_name": last_name,
                "snippet_count": len(chunks),
                "today_iso": today_iso,
                "scheduling_context_present": scheduling_context is not None,
            },
        )
        try:
            answer, grounded_capture = await self._llm.answer_grounded(
                question=question,
                snippets=chunks,
                today_iso=today_iso,
                persona_first_name=first_name,
                persona_last_name=last_name,
                system_prompt_template=grounding_template,
                scheduling_context=scheduling_context,
                project_id=ctx.project_id,
                trace_id=ctx.trace_id,
            )
        except Exception as exc:
            # Error row is fired from inside _chat; answerer does not double-write.
            return self._skip(
                reason="llm_generator_error",
                ctx=ctx,
                question=question,
                chunks=chunks,
                error=repr(exc),
            )

        is_sentinel = answer.strip().upper() == _SENTINEL
        logger.info(
            "grounded_rag_llm_response",
            extra={
                "trace_id": ctx.trace_id,
                "answer_length": len(answer),
                "answer_snippet": answer[:_ANSWER_SNIPPET_MAX],
                "is_sentinel": is_sentinel,
            },
        )
        if is_sentinel:
            self._record_llm_row(grounded_capture, ctx, "escalated_to_hitl")
            return self._skip(
                reason="escalate_sentinel",
                ctx=ctx,
                question=question,
                chunks=chunks,
            )

        try:
            verdict, verifier_capture = await self._llm.verify_grounding(
                question=question,
                answer=answer,
                snippets=chunks,
                system_prompt=verifier_prompt,
                scheduling_context=scheduling_context,
                project_id=ctx.project_id,
                trace_id=ctx.trace_id,
            )
        except Exception as exc:
            self._record_llm_row(grounded_capture, ctx, "error")
            return self._skip(
                reason="verifier_error",
                ctx=ctx,
                question=question,
                chunks=chunks,
                error=repr(exc),
            )
        logger.info(
            "grounded_rag_verifier_result",
            extra={
                "trace_id": ctx.trace_id,
                "verdict_label": verdict.label,
                "verdict_reason": verdict.reason,
            },
        )
        if verdict.label != "GROUNDED":
            self._record_llm_row(grounded_capture, ctx, "verifier_rejected")
            self._record_llm_row(verifier_capture, ctx, "verifier_rejected")
            return self._skip(
                reason="verifier_not_grounded",
                ctx=ctx,
                question=question,
                chunks=chunks,
                verdict_label=verdict.label,
                verdict_reason=verdict.reason,
            )

        decision = evaluate_suggestion(
            answer,
            hedge_phrases=hedge_lines,
            policy_phrases=policy_lines,
        )
        logger.info(
            "grounded_rag_guardrail_result",
            extra={
                "trace_id": ctx.trace_id,
                "valid": decision.valid,
                "score": decision.score,
                "failure_reasons": list(decision.reasons),
            },
        )
        if not decision.valid:
            self._record_llm_row(grounded_capture, ctx, "guardrails_blocked")
            self._record_llm_row(verifier_capture, ctx, "guardrails_blocked")
            return self._skip(
                reason="guardrail_invalid",
                ctx=ctx,
                question=question,
                chunks=chunks,
                guardrail_score=decision.score,
                guardrail_failure_reasons=list(decision.reasons),
            )

        contains_profanity = get_russian_normalizer().contains_profanity(
            answer, custom_lemmas=profanity_lines
        )
        logger.info(
            "grounded_rag_profanity_result",
            extra={
                "trace_id": ctx.trace_id,
                "contains_profanity": contains_profanity,
            },
        )
        if contains_profanity:
            self._record_llm_row(grounded_capture, ctx, "guardrails_blocked")
            self._record_llm_row(verifier_capture, ctx, "guardrails_blocked")
            return self._skip(
                reason="profanity_detected",
                ctx=ctx,
                question=question,
                chunks=chunks,
            )

        self._record_llm_row(grounded_capture, ctx, "customer_visible_answer")
        self._record_llm_row(verifier_capture, ctx, "customer_visible_answer")
        text = answer.strip()
        logger.info(
            "grounded_rag_delivered",
            extra={
                "trace_id": ctx.trace_id,
                "text_length": len(text),
                "retrieval_source_ids": [c.source_id for c in chunks],
                "guardrail_score": decision.score,
            },
        )
        return AnswerResult(
            handled=True,
            text=text,
            response_mode="grounded_rag",
            metadata={
                "retrieval": [_render_chunk_metadata(chunk) for chunk in chunks],
                "verifier": verdict.reason,
                "guardrail_score": decision.score,
            },
        )

    def _record_llm_row(
        self, capture: LlmUsageCapture, ctx: AnswerContext, call_outcome: str
    ) -> None:
        if self._recorder is None or ctx.project_id is None:
            return
        asyncio.create_task(
            self._recorder.record(
                tracker_type="llm",
                project_id=ctx.project_id,
                payload={
                    "model_name": capture.model_name,
                    "prompt_tokens": capture.prompt_tokens,
                    "completion_tokens": capture.completion_tokens,
                    "cost_usd": capture.cost_usd,
                    "call_outcome": call_outcome,
                    "trace_id": ctx.trace_id,
                    "created_at": capture.created_at,
                },
                trace_id=ctx.trace_id,
            )
        )

    def _skip(
        self,
        *,
        reason: str,
        ctx: AnswerContext,
        question: str,
        chunks: list[RagChunk],
        **extra: Any,
    ) -> AnswerResult:
        payload: dict[str, Any] = {
            "trace_id": ctx.trace_id,
            "reason": reason,
            "query": question,
            "threshold": ctx.grounding_threshold,
            "retrieved_count": len(chunks),
            "top_score": chunks[0].score if chunks else None,
            "chunk_source_ids": [chunk.source_id for chunk in chunks],
            "chunk_confidential_flags": [chunk.is_confidential for chunk in chunks],
            "chunk_project_ids": [chunk.project_id for chunk in chunks],
        }
        payload.update(extra)
        logger.info("grounded_rag_skipped", extra=payload)
        return AnswerResult(handled=False)


def _render_chunk_metadata(chunk: RagChunk) -> dict[str, object]:
    if chunk.is_confidential:
        return {
            "source_id": "knowledge_candidate:confidential",
            "chunk_text": "[redacted]",
            "score": chunk.score,
            "is_confidential": True,
        }
    return {
        "source_id": chunk.source_id,
        "chunk_text": chunk.chunk_text,
        "score": chunk.score,
        "is_confidential": False,
    }

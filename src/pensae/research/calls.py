"""Bounded external-call, accounting, and cancellation mechanics."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from uuid import UUID

from pydantic import BaseModel

from pensae.config.protected import ProtectedConfig, ProtectedWorkflowBounds
from pensae.infrastructure.models import ChatResponse, EmbeddingClient, EmbeddingResponse
from pensae.infrastructure.retrieval import RetrievedSource
from pensae.infrastructure.search import SearchBatch, SearchContractError
from pensae.research.budget import BudgetSnapshot, FinalizationBudget, WorkCapacity
from pensae.research.context import ResearchRunContext
from pensae.research.ports import Retriever, Searcher
from pensae.research.roles import (
    PromptLimitError,
    RequiredRoleError,
    RoleExecutionResult,
    RoleSpec,
    StructuredRoleExecutor,
)
from pensae.research.workflow import (
    CancellationProbe,
    CounterDelta,
    ModelCallUsage,
    WorkCounters,
    WorkflowContractFailure,
    WorkflowDependencyUnavailable,
    WorkflowStopRequested,
    WorkLimitExceeded,
)


class BoundedExternalCalls:
    """Shared mechanics for calls that may consume protected run resources."""

    def __init__(self, context: ResearchRunContext) -> None:
        self.context = context
        assert context.effective_bounds is not None
        self._budget = FinalizationBudget.from_policy(
            context.protected.research, context.effective_bounds
        )
        self._budget_stage = "external_call"
        self._achieved_count = 0
        self._admitted_count: int | None = None
        self._evaluated_count = 0
        self._finalization_active = False

    def set_budget_stage(self, stage: str) -> None:
        if not stage:
            raise ValueError("budget stage must be non-empty")
        self._budget_stage = stage

    def admit_upstream(self, *, stage: str) -> BudgetSnapshot:
        """Prove the full configured upstream topology before discovery starts."""

        self.set_budget_stage(stage)
        return self._budget.admit(
            consumed=self._consumed_capacity(),
            request=self._budget.upstream_floor,
            remaining_target_slots=self._budget.target,
            stage=stage,
        )

    def begin_finalization(
        self,
        *,
        achieved_count: int,
        admitted_count: int,
        evaluated_count: int,
        stage: str,
    ) -> BudgetSnapshot:
        """Admit one complete candidate while preserving every later target slot."""

        if self._finalization_active:
            raise RuntimeError("candidate finalization is already active")
        self._validate_achieved_count(achieved_count)
        self._validate_candidate_pool(
            achieved_count=achieved_count,
            admitted_count=admitted_count,
            evaluated_count=evaluated_count,
            require_current=True,
        )
        remaining_after_current = min(
            max(self._budget.target - achieved_count - 1, 0),
            admitted_count - evaluated_count - 1,
        )
        snapshot = self._budget.admit(
            consumed=self._consumed_capacity(),
            request=self._budget.per_slot,
            remaining_target_slots=remaining_after_current,
            stage=stage,
        )
        self._achieved_count = achieved_count
        self._admitted_count = admitted_count
        self._evaluated_count = evaluated_count
        self._finalization_active = True
        self.set_budget_stage(stage)
        return snapshot

    def finish_finalization(
        self,
        *,
        achieved_count: int,
        admitted_count: int,
        evaluated_count: int,
    ) -> BudgetSnapshot:
        """Release the active slot and recalculate reserve after commit or discard."""

        if not self._finalization_active:
            raise RuntimeError("candidate finalization is not active")
        self._validate_achieved_count(achieved_count)
        self._validate_candidate_pool(
            achieved_count=achieved_count,
            admitted_count=admitted_count,
            evaluated_count=evaluated_count,
            require_current=False,
        )
        if self._admitted_count != admitted_count:
            raise ValueError("admitted candidate count changed during finalization")
        if evaluated_count != self._evaluated_count + 1:
            raise ValueError("finalization must evaluate exactly one candidate")
        if achieved_count not in {self._achieved_count, self._achieved_count + 1}:
            raise ValueError("finalization can increase the achieved count by at most one")
        self._achieved_count = achieved_count
        self._evaluated_count = evaluated_count
        self._finalization_active = False
        return self.budget_snapshot()

    def budget_snapshot(self) -> BudgetSnapshot:
        return self._budget.snapshot(
            consumed=self._consumed_capacity(),
            remaining_target_slots=self._reserved_slots(),
        )

    @property
    def _run_id(self) -> UUID:
        return self.context.run_id

    @property
    def _protected(self) -> ProtectedConfig:
        return self.context.protected

    @property
    def _search(self) -> Searcher:
        return self.context.search

    @property
    def _retrieval(self) -> Retriever:
        return self.context.retrieval

    @property
    def _roles(self) -> StructuredRoleExecutor:
        return self.context.roles

    @property
    def _embeddings(self) -> EmbeddingClient:
        return self.context.embeddings

    @property
    def _cancellation(self) -> CancellationProbe:
        return self.context.cancellation

    @property
    def _effective_bounds(self) -> ProtectedWorkflowBounds:
        assert self.context.effective_bounds is not None
        return self.context.effective_bounds

    @property
    def _model_usage(self) -> list[ModelCallUsage]:
        return self.context.accounting.model_usage

    @property
    def _model_calls(self) -> int:
        return self.context.accounting.model_calls

    @_model_calls.setter
    def _model_calls(self, value: int) -> None:
        self.context.accounting.model_calls = value

    @property
    def _repairs(self) -> int:
        return self.context.accounting.repairs

    @_repairs.setter
    def _repairs(self, value: int) -> None:
        self.context.accounting.repairs = value

    @property
    def _total_tokens(self) -> int:
        return self.context.accounting.total_tokens

    @_total_tokens.setter
    def _total_tokens(self, value: int) -> None:
        self.context.accounting.total_tokens = value

    @property
    def _accounting(self) -> WorkCounters:
        return self.context.accounting.counters

    @_accounting.setter
    def _accounting(self, value: WorkCounters) -> None:
        self.context.accounting.counters = value

    async def _model[OutputT: BaseModel](
        self,
        spec: RoleSpec[OutputT],
        *,
        trusted_input: Mapping[str, object],
        seed: int,
        timeout_seconds: int,
        untrusted: tuple[Mapping[str, object], ...] = (),
    ) -> RoleExecutionResult[OutputT]:
        bounds = self._effective_bounds
        self._admit(WorkCapacity(model_calls=1))
        if self._model_calls + 1 > bounds.model_calls:
            raise WorkLimitExceeded("protected model call ceiling reached")
        if self._total_tokens >= bounds.total_run_tokens:
            raise WorkLimitExceeded("protected total run token ceiling reached")
        await self._checkpoint()
        calls_before = self._model_calls

        async def before_generation(prompt_tokens: int, output_cap: int, repair: bool) -> None:
            await self._checkpoint()
            self._admit(
                WorkCapacity(
                    model_calls=1,
                    repairs=1 if repair else 0,
                    total_tokens=prompt_tokens + output_cap,
                )
            )
            if self._model_calls + 1 > bounds.model_calls:
                raise WorkLimitExceeded("protected model call ceiling reached")
            if repair and self._repairs + 1 > bounds.repairs:
                raise WorkLimitExceeded("protected repair ceiling reached")
            if self._total_tokens + prompt_tokens + output_cap > bounds.total_run_tokens:
                raise WorkLimitExceeded("protected total run token ceiling would be exceeded")

        async def after_generation(response: ChatResponse, repair: bool) -> None:
            self._record_chat_call(
                role=spec.role.value,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                repair=repair,
                bounds=bounds,
            )

        try:
            async with asyncio.timeout(timeout_seconds):
                result = await self._roles.execute(
                    spec,
                    trusted_input=trusted_input,
                    untrusted_retrieved_data=untrusted,
                    seed=seed,
                    before_model_call=self._checkpoint,
                    after_model_call=self._checkpoint,
                    before_generation=before_generation,
                    after_generation=after_generation,
                )
        except TimeoutError as exc:
            raise WorkflowDependencyUnavailable("chat dependency unavailable") from exc
        except (WorkflowStopRequested, WorkLimitExceeded):
            raise
        except PromptLimitError as exc:
            raise WorkLimitExceeded("structured role token limit reached") from exc
        except RequiredRoleError as exc:
            raise WorkflowContractFailure("structured role contract failed") from exc
        except (OSError, RuntimeError) as exc:
            raise WorkflowDependencyUnavailable("chat dependency unavailable") from exc
        if self._model_calls == calls_before:
            # Test doubles may implement the role protocol without callback support.
            for attempt in range(result.attempts):
                self._record_chat_call(
                    role=spec.role.value,
                    prompt_tokens=result.prompt_tokens if attempt == result.attempts - 1 else 0,
                    completion_tokens=(
                        result.completion_tokens if attempt == result.attempts - 1 else 0
                    ),
                    repair=attempt > 0,
                    bounds=bounds,
                )
        await self._checkpoint()
        return result

    async def _embed(
        self,
        texts: tuple[str, ...],
        bounds: ProtectedWorkflowBounds,
        *,
        role: str,
    ) -> EmbeddingResponse:
        input_cap = self._protected.research.prompt_input_max_tokens
        input_bytes = sum(len(text.encode("utf-8")) for text in texts)
        if input_bytes > input_cap and role == "signal_clustering":
            texts = _fairly_truncate_embedding_batch(texts, byte_limit=input_cap)
            input_bytes = sum(len(text.encode("utf-8")) for text in texts)
        if input_bytes > input_cap:
            raise WorkLimitExceeded(
                "embedding input token limit would be exceeded", limit="total_tokens"
            )
        self._admit(WorkCapacity(model_calls=1, total_tokens=input_cap))
        if self._model_calls + 1 > bounds.model_calls:
            raise WorkLimitExceeded("protected model call ceiling reached")
        if self._total_tokens + input_cap > bounds.total_run_tokens:
            raise WorkLimitExceeded("protected total run token ceiling would be exceeded")
        await self._checkpoint()
        try:
            async with asyncio.timeout(bounds.embedding_timeout_seconds):
                embedded = await self._embeddings.embed(texts)
        except TimeoutError as exc:
            raise WorkflowDependencyUnavailable("embedding dependency unavailable") from exc
        except (OSError, RuntimeError) as exc:
            raise WorkflowDependencyUnavailable("embedding dependency unavailable") from exc
        if embedded.input_tokens > input_cap:
            raise WorkflowContractFailure("embedding exceeded the protected input token limit")
        if self._total_tokens + embedded.input_tokens > bounds.total_run_tokens:
            raise WorkLimitExceeded("protected total run token ceiling exceeded")
        self._apply(CounterDelta(model_calls=1, input_tokens=embedded.input_tokens), bounds)
        self._model_calls += 1
        self._total_tokens += embedded.input_tokens
        self._model_usage.append(
            ModelCallUsage(
                model=self._protected.policy.embedding_model_id,
                role=role,
                call_index=len(self._model_usage) + 1,
                input_tokens=embedded.input_tokens,
                output_tokens=0,
            )
        )
        await self._checkpoint()
        return embedded

    async def _search_call(self, query: str, bounds: ProtectedWorkflowBounds) -> SearchBatch:
        await self._checkpoint()
        self._admit(WorkCapacity(queries=1))
        self._apply(CounterDelta(queries=1), bounds)
        try:
            async with asyncio.timeout(bounds.search_timeout_seconds):
                result = await self._search.search(query, result_limit=bounds.results_per_query)
        except SearchContractError as exc:
            self._apply(CounterDelta(search_retries=exc.retries), bounds)
            raise WorkflowDependencyUnavailable("search dependency unavailable") from exc
        except (OSError, RuntimeError, TimeoutError) as exc:
            raise WorkflowDependencyUnavailable("search dependency unavailable") from exc
        if result.retries > bounds.search_retries:
            raise WorkflowContractFailure("search adapter exceeded the protected retry bound")
        self._apply(
            CounterDelta(
                search_results=len(result.results),
                search_retries=result.retries,
            ),
            bounds,
        )
        await self._checkpoint()
        return result

    async def _retrieval_call(self, url: str, bounds: ProtectedWorkflowBounds) -> RetrievedSource:
        await self._checkpoint()
        self._admit(WorkCapacity(pages=1, bytes=bounds.source_bytes))
        if self._accounting.retrieved_pages >= bounds.run_pages:
            raise WorkLimitExceeded("protected retrieved_pages ceiling reached")
        remaining_bytes = bounds.run_bytes - self._accounting.retrieved_bytes
        if remaining_bytes <= 0:
            raise WorkLimitExceeded("protected retrieved_bytes ceiling reached")
        try:
            async with asyncio.timeout(bounds.search_timeout_seconds):
                result = await self._retrieval.fetch(url)
        except TimeoutError as exc:
            raise WorkflowDependencyUnavailable("retrieval dependency unavailable") from exc
        except (OSError, RuntimeError) as exc:
            raise WorkflowDependencyUnavailable("retrieval dependency unavailable") from exc
        recorded_bytes = min(result.response_bytes, bounds.source_bytes, remaining_bytes)
        self._apply(CounterDelta(retrieved_pages=1, retrieved_bytes=recorded_bytes), bounds)
        if result.response_bytes > bounds.source_bytes:
            raise WorkLimitExceeded("protected source byte ceiling exceeded")
        if result.response_bytes > remaining_bytes:
            raise WorkLimitExceeded("protected run byte ceiling exceeded")
        await self._checkpoint()
        return result

    def _record_chat_call(
        self,
        *,
        role: str,
        prompt_tokens: int,
        completion_tokens: int,
        repair: bool,
        bounds: ProtectedWorkflowBounds,
    ) -> None:
        if prompt_tokens > self._protected.research.prompt_input_max_tokens:
            raise WorkLimitExceeded("structured role token limit exceeded")
        if self._model_calls + 1 > bounds.model_calls:
            raise WorkLimitExceeded("protected model call ceiling reached")
        if repair and self._repairs + 1 > bounds.repairs:
            raise WorkLimitExceeded("protected repair ceiling reached")
        if self._total_tokens + prompt_tokens + completion_tokens > bounds.total_run_tokens:
            raise WorkLimitExceeded("protected total run token ceiling exceeded")
        self._apply(
            CounterDelta(
                model_calls=1,
                repairs=1 if repair else 0,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
            ),
            bounds,
        )
        self._model_calls += 1
        if repair:
            self._repairs += 1
        self._total_tokens += prompt_tokens + completion_tokens
        self._model_usage.append(
            ModelCallUsage(
                model=self._protected.policy.chat_model_id,
                role=role,
                call_index=len(self._model_usage) + 1,
                repair=repair,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
            )
        )

    def _apply(self, delta: CounterDelta, bounds: ProtectedWorkflowBounds) -> None:
        self._accounting = self._accounting.increment(delta, bounds)

    def _admit(self, request: WorkCapacity) -> BudgetSnapshot:
        return self._budget.admit(
            consumed=self._consumed_capacity(),
            request=request,
            remaining_target_slots=self._reserved_slots(),
            stage=self._budget_stage,
        )

    def _consumed_capacity(self) -> WorkCapacity:
        return WorkCapacity.from_counters(self._accounting)

    def _reserved_slots(self) -> int:
        if self._admitted_count is None:
            return self._budget.target
        current_slot = 1 if self._finalization_active else 0
        return min(
            max(self._budget.target - self._achieved_count - current_slot, 0),
            max(self._admitted_count - self._evaluated_count - current_slot, 0),
        )

    def _validate_achieved_count(self, achieved_count: int) -> None:
        if not 0 <= achieved_count <= self._budget.target:
            raise ValueError("achieved count is outside the protected target")

    def _validate_candidate_pool(
        self,
        *,
        achieved_count: int,
        admitted_count: int,
        evaluated_count: int,
        require_current: bool,
    ) -> None:
        if not 0 <= evaluated_count <= admitted_count <= self._budget.configured_concepts:
            raise ValueError("candidate counts are outside the configured concept pool")
        if achieved_count > evaluated_count:
            raise ValueError("achieved count cannot exceed evaluated candidates")
        if require_current and evaluated_count >= admitted_count:
            raise ValueError("candidate finalization requires an unevaluated admitted candidate")

    async def _checkpoint(self) -> None:
        if await self._cancellation.requested(self._run_id):
            raise WorkflowStopRequested("stop observed at work boundary")


def _fairly_truncate_embedding_batch(texts: tuple[str, ...], *, byte_limit: int) -> tuple[str, ...]:
    """Keep one deterministic non-empty clustering input per signal under one batch cap."""

    if not texts or byte_limit < len(texts):
        raise WorkLimitExceeded(
            "embedding input token limit would be exceeded", limit="total_tokens"
        )
    per_text = byte_limit // len(texts)
    return tuple(_utf8_prefix(text, per_text) for text in texts)


def _utf8_prefix(text: str, byte_limit: int) -> str:
    raw = text.encode("utf-8")[:byte_limit]
    while raw:
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw[:-1]
            continue
        if value:
            return value
        break
    raise WorkLimitExceeded("embedding input token limit would be exceeded", limit="total_tokens")

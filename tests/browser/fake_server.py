from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from tests.fakes.health import fake_health_service

from pensae.api.app import create_app
from pensae.config.protected import ProtectedConfig
from pensae.config.settings import BootstrapSettings, Environment
from pensae.opportunities import (
    DeletionResult,
    EvidenceDetail,
    LifecycleMutationResult,
    LifecycleTarget,
    LifecycleTargetPage,
    OpportunityDetail,
    OpportunityVersionSummary,
    PortfolioItem,
    PortfolioQuery,
    ProblemPatternDetail,
    ProblemSignalDetail,
    RediscoveryDecision,
    RelatedOpportunityDetail,
    RunDetail,
    StableOpportunityMetadata,
)
from pensae.opportunities.records import RunNonCountingOutcomes
from pensae.research.schemas import ClaimAssessment, OpportunityAnalysis, ProposedScores
from pensae.research.workflow import WorkCounters
from pensae.runs.control import ProgressEvent, ProgressRecord, ProgressReplay
from pensae.runs.service import ResearchService
from pensae.settings import (
    DurableSettingsValue,
    FutureRunSettingsSnapshot,
    ResetSettingsRequest,
    SavedSettings,
    SavedSettingsService,
)

RUN_ID = UUID("00000000-0000-0000-0000-000000000010")
OPPORTUNITY_ID = UUID("00000000-0000-0000-0000-000000000020")
VERSION_ID = UUID("00000000-0000-0000-0000-000000000021")
HISTORICAL_VERSION_ID = UUID("00000000-0000-0000-0000-000000000022")
ZERO_COMMIT_FOCUS = "Pensae Signal browser zero-commit regression"
FIVE_OPPORTUNITY_FOCUS = "Pensae Signal browser five-opportunity acceptance"
SHORTFALL_FOCUS = "Pensae Signal browser honest-shortfall acceptance"
P7_OPPORTUNITY_IDS = (
    OPPORTUNITY_ID,
    UUID("00000000-0000-0000-0000-000000000023"),
    UUID("00000000-0000-0000-0000-000000000024"),
    UUID("00000000-0000-0000-0000-000000000025"),
    UUID("00000000-0000-0000-0000-000000000026"),
)
P7_OPPORTUNITY_NAMES = (
    "Maintenance Request Triage Assistant",
    "Vendor Dispatch Evidence Desk",
    "Tenant Update Coordination Queue",
    "Inspection Follow-up Workbench",
    "Portfolio Repair Pattern Monitor",
)


class BrowserProgress:
    def __init__(self) -> None:
        self.records: dict[UUID, list[ProgressRecord]] = {}

    async def initialize(self, run_id: UUID) -> None:
        self.records[run_id] = []

    async def publish(self, event: ProgressEvent) -> str:
        items = self.records.setdefault(event.run_id, [])
        event_id = f"{len(items) + 1}-0"
        items.append(ProgressRecord(event_id=event_id, event=event))
        return event_id

    async def request_stop(self, run_id: UUID) -> None:
        del run_id

    async def is_stop_requested(self, run_id: UUID) -> bool:
        del run_id
        return False

    async def replay(self, run_id: UUID, after_id: str | None) -> ProgressReplay:
        records = tuple(self.records.get(run_id, []))
        if after_id is None:
            return ProgressReplay(records, False)
        for index, record in enumerate(records):
            if record.event_id == after_id:
                return ProgressReplay(records[index + 1 :], False)
        return ProgressReplay((), True)


class BrowserStore:
    def __init__(self) -> None:
        self.runs: dict[UUID, RunDetail] = {}
        self.favorite = False
        self.note: str | None = None
        self.revision = 1
        self.lifecycle_status = "active"
        self.classification = "possible_rediscovery"
        self.merge_target_id: UUID | None = None
        self.rediscovery_target_id: UUID | None = None
        self.deleted = False

    async def create_run(self, snapshot: Any) -> UUID:
        now = datetime.now(UTC)
        self.runs[snapshot.id] = RunDetail(
            id=snapshot.id,
            state="running",
            opportunity_id=None,
            effective_config=snapshot.effective_config,
            workflow_version=snapshot.workflow_version,
            schema_version=snapshot.schema_version,
            created_at=now,
            updated_at=now,
        )
        return snapshot.id

    async def get_run(self, run_id: UUID) -> RunDetail | None:
        return self.runs.get(run_id)

    async def get_detail(self, opportunity_id: UUID) -> OpportunityDetail | None:
        return await browser_manager.get_opportunity(opportunity_id)

    async def get_version_detail(
        self, opportunity_id: UUID, version_id: UUID
    ) -> OpportunityDetail | None:
        detail = await browser_manager.get_opportunity(opportunity_id)
        if detail is None or version_id not in {VERSION_ID, HISTORICAL_VERSION_ID}:
            return None
        if version_id == HISTORICAL_VERSION_ID:
            return detail.model_copy(
                update={"version_id": HISTORICAL_VERSION_ID, "version_number": 1}
            )
        return detail


class BrowserManager:
    def __init__(self, store: BrowserStore, progress: BrowserProgress) -> None:
        self.store = store
        self.progress = progress
        self._active: UUID | None = None
        self._transient_terminal: RunDetail | None = None
        self._stop: set[UUID] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def active_run_id(self) -> UUID | None:
        return self._active

    def get_transient_terminal(self, run_id: UUID) -> RunDetail | None:
        snapshot = self._transient_terminal
        return snapshot if snapshot is not None and snapshot.id == run_id else None

    async def start(self, snapshot: Any) -> UUID:
        await self.store.create_run(snapshot)
        await self.progress.initialize(snapshot.id)
        self._transient_terminal = None
        self._active = snapshot.id
        await self._publish(snapshot.id, "run_started", "created")
        saved = cast(dict[str, Any], snapshot.effective_config.get("saved_settings", {}))
        research = cast(dict[str, Any], saved.get("research", {}))
        focus = research.get("focus")
        task = asyncio.create_task(
            self._drive(
                snapshot.id,
                zero_commit=focus == ZERO_COMMIT_FOCUS,
                p7_mode=(
                    "five"
                    if focus == FIVE_OPPORTUNITY_FOCUS
                    else "shortfall"
                    if focus == SHORTFALL_FOCUS
                    else None
                ),
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return snapshot.id

    async def stop(self, run_id: UUID) -> bool:
        if self._active != run_id:
            return False
        self._stop.add(run_id)
        detail = self.store.runs[run_id]
        self.store.runs[run_id] = detail.model_copy(
            update={"state": "stopping", "current_stage": "stopping"}
        )
        await self._publish(run_id, "stopping", "stopping", state="stopping")
        return True

    async def _drive(
        self,
        run_id: UUID,
        *,
        zero_commit: bool = False,
        p7_mode: str | None = None,
    ) -> None:
        if p7_mode is not None:
            await self._drive_p7(run_id, shortfall=p7_mode == "shortfall")
            return
        if zero_commit:
            await asyncio.sleep(0.2)
            counters = WorkCounters(
                queries=6,
                search_results=60,
                unique_urls=60,
                model_calls=1,
                input_tokens=148,
                output_tokens=148,
            )
            detail = self.store.runs[run_id]
            self.store.runs[run_id] = detail.model_copy(
                update={
                    "current_stage": "preliminary_gate",
                    "work_counters": asdict(counters),
                }
            )
            await self._publish(run_id, "stage_completed", "preliminary_gate", counters=counters)
            await asyncio.sleep(0.2)
            await self._terminal(
                run_id,
                "completed_with_warnings",
                counters,
                warning_code="no_gate_survivor",
            )
            return
        stages = (
            "discovery_search",
            "signal_extraction",
            "focused_retrieval",
            "similarity_commit",
        )
        counters = WorkCounters()
        for index, stage in enumerate(stages, start=1):
            await asyncio.sleep(0.2)
            if run_id in self._stop:
                await self._terminal(run_id, "stopped", counters)
                return
            counters = WorkCounters(
                queries=min(index * 2, 8),
                retrieved_pages=index * 2,
                signals=max(0, index - 1),
                model_calls=index,
                input_tokens=index * 100,
                output_tokens=index * 30,
                opportunities=1 if stage == "similarity_commit" else 0,
            )
            detail = self.store.runs[run_id]
            update: dict[str, Any] = {
                "current_stage": stage,
                "work_counters": asdict(counters),
                "target_count": 5,
                "admitted_count": 1 if stage in {"focused_retrieval", "similarity_commit"} else 0,
                "evaluated_count": 1 if stage == "similarity_commit" else 0,
                "achieved_count": 1 if stage == "similarity_commit" else 0,
                "committed_count": 1 if stage == "similarity_commit" else 0,
            }
            self.store.runs[run_id] = detail.model_copy(update=update)
            await self._publish(run_id, "stage_completed", stage, counters=counters)
        await asyncio.sleep(0.6)
        await self._terminal(
            run_id,
            "stopped" if run_id in self._stop else "completed",
            counters,
        )

    async def _drive_p7(self, run_id: UUID, *, shortfall: bool) -> None:
        target = 5
        commit_target = 3 if shortfall else target
        counters = WorkCounters()
        for index in range(1, commit_target + 1):
            await asyncio.sleep(0.15)
            if run_id in self._stop:
                await self._terminal(run_id, "stopped", counters)
                return
            counters = WorkCounters(
                queries=8 + index * 3,
                search_results=80 + index * 10,
                unique_urls=60,
                retrieved_pages=24 + index * 6,
                retrieved_bytes=(24 + index * 6) * 16_384,
                signals=20,
                patterns=8,
                segments=12,
                preliminary_survivors=8,
                concepts=8,
                opportunities=index,
                model_calls=30 + index * 5,
                repairs=index,
                input_tokens=40_000 + index * 20_000,
                output_tokens=8_000 + index * 4_000,
            )
            detail = self.store.runs[run_id]
            committed_ids = P7_OPPORTUNITY_IDS[:index]
            self.store.runs[run_id] = detail.model_copy(
                update={
                    "current_stage": "similarity_commit",
                    "work_counters": asdict(counters),
                    "target_count": target,
                    "admitted_count": index,
                    "evaluated_count": index,
                    "achieved_count": index,
                    "committed_count": index,
                    "opportunity_id": committed_ids[-1],
                    "committed_opportunity_ids": committed_ids,
                }
            )
            await self._publish(run_id, "opportunity_committed", "similarity_commit", counters)
        await asyncio.sleep(0.15)
        if shortfall:
            detail = self.store.runs[run_id]
            self.store.runs[run_id] = detail.model_copy(
                update={
                    "admitted_count": 5,
                    "evaluated_count": 5,
                    "non_counting_outcomes": RunNonCountingOutcomes(
                        invalid_candidate=1,
                        incomplete_candidate=1,
                    ),
                    "shortfall_code": "insufficient_evidence",
                    "shortfall_detail": (
                        "Available verified evidence could not support five complete "
                        "opportunities. Pensae Signal did not weaken an evidence gate or add "
                        "filler."
                    ),
                    "warning_codes": ("opportunity_target_shortfall",),
                }
            )
            await self._terminal(
                run_id,
                "completed_with_warnings",
                counters,
                warning_code="opportunity_target_shortfall",
            )
            return
        await self._terminal(run_id, "completed", counters)

    async def _terminal(
        self,
        run_id: UUID,
        state: str,
        counters: WorkCounters,
        *,
        warning_code: str | None = None,
    ) -> None:
        detail = self.store.runs[run_id]
        terminal = detail.model_copy(
            update={
                "state": state,
                "current_stage": "terminal_cleanup",
                "opportunity_id": (
                    detail.committed_opportunity_ids[-1]
                    if detail.committed_opportunity_ids
                    else OPPORTUNITY_ID
                    if detail.committed_count
                    else None
                ),
                "warning_codes": (
                    tuple(dict.fromkeys((*detail.warning_codes, warning_code)))
                    if warning_code
                    else detail.warning_codes
                ),
            }
        )
        if terminal.committed_count:
            self.store.runs[run_id] = terminal
        else:
            self._transient_terminal = terminal
            del self.store.runs[run_id]
        await self._publish(
            run_id,
            "terminal",
            "terminal_cleanup",
            counters,
            state,
            committed_count=terminal.committed_count,
            warning_code=warning_code,
        )
        if self._active == run_id:
            self._active = None

    async def _publish(
        self,
        run_id: UUID,
        kind: Any,
        stage: str,
        counters: WorkCounters | None = None,
        state: Any = "running",
        committed_count: int | None = None,
        warning_code: str | None = None,
    ) -> None:
        detail = self.store.runs.get(run_id)
        await self.progress.publish(
            ProgressEvent(
                run_id=run_id,
                kind=kind,
                state=state,
                stage=stage,
                counters=counters or WorkCounters(),
                target_count=detail.target_count if detail is not None else 5,
                admitted_count=detail.admitted_count if detail is not None else 0,
                evaluated_count=detail.evaluated_count if detail is not None else 0,
                achieved_count=detail.achieved_count if detail is not None else 0,
                committed_count=(
                    committed_count
                    if committed_count is not None
                    else detail.committed_count
                    if detail is not None
                    else 0
                ),
                non_counting_outcomes=(
                    detail.non_counting_outcomes if detail is not None else RunNonCountingOutcomes()
                ),
                limit_code=detail.limit_code if detail is not None else None,
                limit_stage=detail.limit_stage if detail is not None else None,
                shortfall_code=detail.shortfall_code if detail is not None else None,
                warning_code=warning_code,
            )
        )

    async def get_opportunity(self, opportunity_id: UUID) -> OpportunityDetail | None:
        if opportunity_id not in P7_OPPORTUNITY_IDS or self.store.deleted:
            return None
        opportunity_index = P7_OPPORTUNITY_IDS.index(opportunity_id)
        opportunity_name = P7_OPPORTUNITY_NAMES[opportunity_index]
        return OpportunityDetail(
            id=opportunity_id,
            version_id=UUID(int=VERSION_ID.int + opportunity_index * 10),
            version_number=2,
            primary_industry="Property management",
            revision=self.store.revision,
            lifecycle_status=cast(Any, self.store.lifecycle_status),
            classification=cast(Any, self.store.classification),
            merge_target_id=self.store.merge_target_id,
            rediscovery_target_id=self.store.rediscovery_target_id,
            favorite=browser_store.favorite,
            note=browser_store.note,
            report=OpportunityAnalysis(
                opportunity_name=opportunity_name,
                concise_summary="A focused intake and follow-up layer.",
                primary_industry="Property management",
                problem_pattern="Fragmented channels delay maintenance triage.",
                target_segment="Small property managers",
                affected_user="Property managers and maintenance coordinators",
                likely_buyer="Head of property operations",
                recurring_workflow="Receive, triage, assign, and follow up on requests",
                current_workaround="Email, text messages, and shared spreadsheets",
                business_consequences=("Delayed repairs", "Inconsistent tenant updates"),
                proposed_solution="An auditable intake and follow-up queue.",
                delivery_model="Locally operated web application",
                initial_market_reason="The recurring coordination burden is well bounded.",
                frequency_value_hypothesis="Daily delays consume staff time and tenant trust.",
                other_segments=("Small facilities-management teams",),
                alternatives=("Property-management suites", "Shared inbox and spreadsheet"),
                missing_capabilities=("Direct work-order integration",),
                reusable_core_capabilities=("Channel intake", "Triage queue", "Audit trail"),
                pricing_or_spend_signals=("Teams already fund software and coordination time.",),
                market_saturation="Broad suites exist, but the focused workflow gap persists.",
                incumbent_response_risk="Incumbents could add a similar focused workflow.",
                integration_customization_burden="The first version avoids deep integrations.",
                preferred_technology_fit="A local web app fits this auditable workflow.",
                trust_regulatory_constraints=("Tenant messages may contain personal data.",),
                operational_burden="Source review and support must remain bounded.",
                commercial_analysis="A buyer can connect faster triage to staff time.",
                feasibility_analysis="The first product can remain narrow.",
                differentiation_analysis="Evidence-linked output differs from generic inboxes.",
                risks=("Incumbent suites could add similar automation.",),
                unknowns=("Willingness to pay needs direct validation.",),
                reasons_not_to_pursue=(),
                supporting_evidence_ids=("00000000-0000-0000-0000-000000000030",),
                negative_evidence_ids=(),
                conflicting_evidence_ids=(),
                next_research_questions=("Which request volume creates budget urgency?",),
                claims=(
                    ClaimAssessment(
                        statement="The workflow relies on manual copying.",
                        label="fact",
                        evidence_ids=("00000000-0000-0000-0000-000000000030",),
                    ),
                    ClaimAssessment(
                        statement="A focused queue may reduce delay.", label="inference"
                    ),
                    ClaimAssessment(statement="Teams may pay for saved time.", label="assumption"),
                    ClaimAssessment(
                        statement="Initial weekly savings are approximate.", label="estimate"
                    ),
                    ClaimAssessment(
                        statement="The buyer may fund the workflow.", label="assumption"
                    ),
                    ClaimAssessment(
                        statement="A narrow intake queue is viable.", label="hypothesis"
                    ),
                    ClaimAssessment(
                        statement="Incumbent suites may cover the workflow.",
                        label="conflict",
                        evidence_ids=("00000000-0000-0000-0000-000000000030",),
                    ),
                    ClaimAssessment(
                        statement="Validated willingness to pay is missing.",
                        label="missing_evidence",
                    ),
                ),
                source_diversity_limitation="The example has one retained source.",
                conflict_limitation=None,
                proposed_scores=ProposedScores(
                    commercial_attractiveness=4,
                    commercial_explanation="A plausible operations buyer exists.",
                    evidence_strength=4,
                    evidence_explanation="Independent evidence supports the workflow problem.",
                    pensae_feasibility=4,
                    feasibility_explanation="The initial workflow avoids deep integration.",
                    differentiation=3,
                    differentiation_explanation="Incumbent response remains a risk.",
                ),
                strong_evidence=True,
                plausible_buyer=True,
                payment_or_value_path=True,
                critical_blocker=False,
                strong_negative_evidence=False,
                implausible_economics=False,
                excessive_customization_or_operations=False,
            ),
            scores={
                "commercial_attractiveness": Decimal(4),
                "evidence_strength": Decimal(4),
                "pensae_feasibility": Decimal(4),
                "differentiation": Decimal(3),
                "weighted": Decimal("3.90"),
            },
            verdict="promising",
            evidence=(
                EvidenceDetail(
                    id=UUID("00000000-0000-0000-0000-000000000030"),
                    excerpt="Staff copy maintenance status into a shared spreadsheet.",
                    supported_claim="The workflow relies on manual copying.",
                    evidence_kind="supporting",
                    source_url="https://example.test/source",
                    source_title="Synthetic maintenance workflow source",
                    publisher="Offline fixture",
                    retrieved_at=datetime(2026, 7, 22, tzinfo=UTC),
                ),
            ),
            provenance={
                "workflow_version": "phase1.vertical-slice.v1",
                "schema_version": "phase1.opportunity.v1",
                "chat_model_id": "offline-fake-chat",
                "embedding_model_id": "offline-fake-embedding",
            },
            origin_pattern=ProblemPatternDetail(
                id=UUID("00000000-0000-0000-0000-000000000040"),
                summary="Fragmented channels delay maintenance triage.",
            ),
            origin_signals=(
                ProblemSignalDetail(
                    id=UUID("00000000-0000-0000-0000-000000000041"),
                    affected_user="Property managers",
                    recurring_workflow="Triage incoming maintenance requests",
                    current_workaround="Copy messages into a shared spreadsheet",
                    business_consequence="Repairs and tenant updates are delayed",
                    confidence=Decimal("0.91"),
                ),
            ),
            related=(
                RelatedOpportunityDetail(
                    opportunity_id=UUID("00000000-0000-0000-0000-000000000050"),
                    opportunity_name="Facilities Intake Queue",
                    primary_industry="Facilities management",
                    similarity=Decimal("0.84"),
                    relation_kind="related",
                    revision=2,
                    lifecycle_status="active",
                ),
            ),
            versions=(
                OpportunityVersionSummary(
                    id=HISTORICAL_VERSION_ID,
                    version_number=1,
                    verdict="needs_more_evidence",
                    weighted_score=Decimal("3.40"),
                    evidence_score=3,
                    created_at=datetime(2026, 7, 20, tzinfo=UTC),
                    is_current=False,
                ),
                OpportunityVersionSummary(
                    id=VERSION_ID,
                    version_number=2,
                    verdict="promising",
                    weighted_score=Decimal("3.90"),
                    evidence_score=4,
                    created_at=datetime(2026, 7, 22, tzinfo=UTC),
                    is_current=True,
                ),
            ),
        )


class BrowserPortfolio:
    async def list(self, query: PortfolioQuery) -> tuple[PortfolioItem, ...]:
        if query.industry and query.industry.casefold() != "property management":
            return ()
        candidate = (
            ()
            if browser_store.deleted
            else (
                PortfolioItem(
                    id=OPPORTUNITY_ID,
                    version_id=VERSION_ID,
                    opportunity_name="Maintenance Request Triage Assistant",
                    concise_summary="A focused intake and follow-up layer.",
                    primary_industry="Property management",
                    favorite=browser_store.favorite,
                    note=browser_store.note,
                    revision=browser_store.revision,
                    lifecycle_status=browser_store.lifecycle_status,
                    classification=browser_store.classification,
                    merge_target_id=browser_store.merge_target_id,
                    verdict="promising",
                    weighted_score=Decimal("3.90"),
                    evidence_score=4,
                    version_created_at=datetime(2026, 7, 22, tzinfo=UTC),
                ),
            )
        )
        if query.industry:
            return candidate
        return (
            *candidate,
            PortfolioItem(
                id=UUID("00000000-0000-0000-0000-000000000050"),
                version_id=UUID("00000000-0000-0000-0000-000000000051"),
                opportunity_name="Facilities Intake Queue",
                concise_summary="Existing stable opportunity for lifecycle review.",
                primary_industry="Facilities management",
                favorite=False,
                note=None,
                revision=2,
                lifecycle_status="active",
                classification="new",
                verdict="needs_more_evidence",
                weighted_score=Decimal("3.20"),
                evidence_score=3,
                version_created_at=datetime(2026, 7, 20, tzinfo=UTC),
            ),
        )

    async def lifecycle_targets(
        self, source_id: UUID, *, search: str | None, limit: int
    ) -> LifecycleTargetPage:
        assert source_id == OPPORTUNITY_ID
        target = LifecycleTarget(
            id=UUID("00000000-0000-0000-0000-000000000050"),
            opportunity_name="Facilities Intake Queue",
            primary_industry="Facilities management",
            revision=2,
            lifecycle_status="active",
        )
        matches = search is None or search.casefold() in target.opportunity_name.casefold()
        return LifecycleTargetPage(
            items=(target,) if matches else (),
            has_more=False,
            search=search,
            limit=limit,
        )

    async def set_favorite(
        self, opportunity_id: UUID, *, favorite: bool
    ) -> StableOpportunityMetadata:
        if opportunity_id != OPPORTUNITY_ID:
            raise LookupError("opportunity does not exist")
        browser_store.favorite = favorite
        browser_store.revision += 1
        return self._metadata()

    async def set_note(
        self, opportunity_id: UUID, *, note: str | None
    ) -> StableOpportunityMetadata:
        if opportunity_id != OPPORTUNITY_ID:
            raise LookupError("opportunity does not exist")
        browser_store.note = note.strip() if note and note.strip() else None
        browser_store.revision += 1
        return self._metadata()

    @staticmethod
    def _metadata() -> StableOpportunityMetadata:
        return StableOpportunityMetadata(
            id=OPPORTUNITY_ID,
            revision=browser_store.revision,
            favorite=browser_store.favorite,
            note=browser_store.note,
            updated_at=datetime.now(UTC),
        )


class BrowserSettings:
    def __init__(self) -> None:
        self.policy = SavedSettingsService()
        self.current = self.policy.initial_value()

    @property
    def field_metadata(self):
        return self.policy.baseline.field_metadata

    async def get(self) -> DurableSettingsValue:
        return self.current

    async def save(self, candidate: SavedSettings) -> DurableSettingsValue:
        self.current = self.policy.save(self.current, candidate)
        return self.current

    async def reset(self, request: ResetSettingsRequest) -> DurableSettingsValue:
        self.current = self.policy.reset(self.current, request)
        return self.current

    async def snapshot_for_future_run(self) -> FutureRunSettingsSnapshot:
        return self.policy.snapshot_for_future_run(self.current)


class BrowserLifecycle:
    async def decide_rediscovery(
        self,
        opportunity_id: UUID,
        *,
        target_id: UUID,
        decision: RediscoveryDecision,
        expected_candidate_revision: int,
        expected_target_revision: int,
    ) -> LifecycleMutationResult:
        del expected_target_revision
        assert opportunity_id == OPPORTUNITY_ID
        assert expected_candidate_revision == browser_store.revision
        browser_store.revision += 1
        browser_store.classification = decision.value
        browser_store.rediscovery_target_id = (
            None if decision is RediscoveryDecision.RELATED else target_id
        )
        return LifecycleMutationResult(
            opportunity_id=opportunity_id,
            revision=browser_store.revision,
            classification=decision.value,
            lifecycle_status=browser_store.lifecycle_status,
            target_opportunity_id=target_id,
            target_revision=2,
        )

    async def merge(
        self,
        opportunity_id: UUID,
        *,
        survivor_id: UUID,
        expected_revision: int,
        expected_survivor_revision: int,
    ) -> LifecycleMutationResult:
        del expected_survivor_revision
        assert opportunity_id == OPPORTUNITY_ID
        assert expected_revision == browser_store.revision
        browser_store.revision += 1
        browser_store.lifecycle_status = "merged"
        browser_store.merge_target_id = survivor_id
        return LifecycleMutationResult(
            opportunity_id=opportunity_id,
            revision=browser_store.revision,
            classification=browser_store.classification,
            lifecycle_status="merged",
            target_opportunity_id=survivor_id,
            target_revision=2,
        )

    async def reverse_merge(
        self, opportunity_id: UUID, *, expected_revision: int
    ) -> LifecycleMutationResult:
        assert opportunity_id == OPPORTUNITY_ID
        assert expected_revision == browser_store.revision
        browser_store.revision += 1
        former = browser_store.merge_target_id
        browser_store.lifecycle_status = "active"
        browser_store.merge_target_id = None
        return LifecycleMutationResult(
            opportunity_id=opportunity_id,
            revision=browser_store.revision,
            classification=browser_store.classification,
            lifecycle_status="active",
            target_opportunity_id=former,
        )

    async def delete_permanently(
        self, opportunity_id: UUID, *, expected_revision: int
    ) -> DeletionResult:
        assert opportunity_id == OPPORTUNITY_ID
        assert expected_revision == browser_store.revision
        browser_store.deleted = True
        return DeletionResult(opportunity_id=opportunity_id, deleted=True, audit_id=UUID(int=99))


settings = BootstrapSettings.model_validate({"environment": Environment.TEST, "app_port": 8123})
browser_store = BrowserStore()
browser_progress = BrowserProgress()
browser_manager = BrowserManager(browser_store, browser_progress)
browser_portfolio = BrowserPortfolio()
browser_settings = BrowserSettings()
browser_lifecycle = BrowserLifecycle()
app = create_app(
    settings=settings,
    health_service=fake_health_service(),
    research_service=ResearchService(
        manager=cast(Any, browser_manager),
        progress=cast(Any, browser_progress),
        store=cast(Any, browser_store),
        protected=ProtectedConfig.load(),
        settings_store=cast(Any, browser_settings),
    ),
    portfolio_service=cast(Any, browser_portfolio),
    settings_store=cast(Any, browser_settings),
    lifecycle_service=cast(Any, browser_lifecycle),
    frontend_dist=Path("frontend/dist"),
)

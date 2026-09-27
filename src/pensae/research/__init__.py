"""Stable research workflow contracts and role execution."""

from .roles import (
    PromptLimitError,
    RequiredRoleError,
    RoleExecutionResult,
    RoleName,
    RoleSpec,
    StructuredRoleExecutor,
    build_role_specs,
)
from .schemas import (
    ClaimAssessment,
    FocusedEvidenceSelection,
    OpportunityAnalysis,
    ProposedScores,
    QueryPlan,
    SegmentSolution,
    SignalSelection,
)

__all__ = [
    "ClaimAssessment",
    "FocusedEvidenceSelection",
    "OpportunityAnalysis",
    "PromptLimitError",
    "ProposedScores",
    "QueryPlan",
    "RequiredRoleError",
    "RoleExecutionResult",
    "RoleName",
    "RoleSpec",
    "SegmentSolution",
    "SignalSelection",
    "StructuredRoleExecutor",
    "build_role_specs",
]

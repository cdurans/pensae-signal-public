"""Small deterministic helpers shared by research capabilities."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

from pensae.research.roles import RoleExecutionResult
from pensae.research.schemas import PatternSynthesis
from pensae.research.workflow import CounterDelta, WorkflowContractFailure


def model_delta[OutputT: BaseModel](result: RoleExecutionResult[OutputT]) -> CounterDelta:
    return CounterDelta(
        model_calls=result.attempts,
        repairs=max(0, result.attempts - 1),
        input_tokens=result.prompt_tokens,
        output_tokens=result.completion_tokens,
    )


def add_usage[OutputT: BaseModel](
    result: RoleExecutionResult[OutputT],
    calls: int,
    repairs: int,
    input_tokens: int,
    output_tokens: int,
) -> tuple[int, int, int, int]:
    return (
        calls + result.attempts,
        repairs + max(0, result.attempts - 1),
        input_tokens + result.prompt_tokens,
        output_tokens + result.completion_tokens,
    )


def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        raise WorkflowContractFailure("pattern embedding dimensions are inconsistent")
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        raise WorkflowContractFailure("pattern embeddings must be nonzero")
    return numerator / (left_norm * right_norm)


def strongest_pattern_similarity(
    current_similarity: float, retained_similarities: tuple[float, ...]
) -> float:
    """Combine current-run and retained comparisons without hiding either source."""

    return max((current_similarity, *retained_similarities))


def pattern_fingerprint(value: PatternSynthesis, *, version: str) -> str:
    payload = {
        "version": version,
        **{
            key: " ".join(text.casefold().split())
            for key, text in value.model_dump(mode="python").items()
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.fakes.models import FakeChatClient

from pensae.config.protected import ProtectedConfig
from pensae.infrastructure.models import ChatResponse
from pensae.research.roles import (
    RequiredRoleError,
    RoleName,
    StructuredRoleExecutor,
    build_role_specs,
)
from pensae.research.schemas import QueryPlan
from pensae.research.workflow import WORKFLOW_SEQUENCE


def _response(content: str) -> ChatResponse:
    return ChatResponse(content=content, prompt_tokens=12, completion_tokens=6)


@pytest.mark.anyio
@pytest.mark.parametrize("case_index", [0, 1, 2])
async def test_schema_cases_allow_exactly_one_repair_and_then_fail_closed(
    case_index: int, corpus: dict[str, Any]
) -> None:
    case = corpus["schema_repair_cases"][case_index]
    role = RoleName(case["role"])
    spec = build_role_specs(ProtectedConfig.load().research)[role]
    assert spec.required is case["required"]
    fake = FakeChatClient(tuple(_response(value) for value in case["responses"]))
    executor = StructuredRoleExecutor(chat=fake)

    if case["expected_outcome"] == "required_failure":
        with pytest.raises(RequiredRoleError, match="after one repair"):
            await executor.execute(spec, trusted_input={}, seed=70)
    else:
        result = await executor.execute(spec, trusted_input={}, seed=70)
        assert result.attempts == case["expected_attempts"]
        if case["expected_outcome"] == "repaired":
            assert result.value is not None
        else:
            assert case["expected_outcome"] == "discarded"
            assert result.value is None

    assert len(fake.requests) == case["expected_attempts"]
    assert sum(request.seed == 71 for request in fake.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("case_index", [0, 1])
async def test_prompt_injection_cases_remain_data_and_are_not_followed(
    case_index: int,
    corpus: dict[str, Any],
    sources: dict[str, dict[str, Any]],
) -> None:
    case = corpus["prompt_injection_cases"][case_index]
    source = sources[case["source_id"]]
    payload = case.get("payload_override", source["text"])
    response_text = json.dumps(case["safe_response"], separators=(",", ":"))
    fake = FakeChatClient((_response(response_text),))
    spec = build_role_specs(ProtectedConfig.load().research)[RoleName.RESEARCH_PLANNER]

    result = await StructuredRoleExecutor(chat=fake).execute(
        spec,
        trusted_input={"focus": "recurring operations problems"},
        untrusted_retrieved_data=({"source_id": source["id"], "text": payload},),
        seed=90,
    )

    assert isinstance(result.value, QueryPlan)
    rendered_output = " ".join(result.value.queries)
    assert all(marker not in rendered_output for marker in case["attack_markers"])
    user_message = fake.requests[0].messages[1].content
    assert user_message.count("</untrusted_retrieved_data_json>") == 1
    if "<" in payload:
        assert "\\u003c" in user_message
        assert "<system>" not in user_message


def test_role_and_fixed_workflow_inventory_matches_the_corpus(corpus: dict[str, Any]) -> None:
    policy = ProtectedConfig.load().research
    specs = build_role_specs(policy)
    expected_roles = corpus["workflow_contract"]["roles"]

    assert [node.value for node in WORKFLOW_SEQUENCE] == corpus["workflow_contract"]["sequence"]
    assert {role.value for role in specs} == {item["name"] for item in expected_roles}
    for item in expected_roles:
        spec = specs[RoleName(item["name"])]
        assert spec.output_type.__name__ == item["schema"]
        assert spec.required is item["required"]
    assert set(corpus["workflow_contract"]) == {
        "roles",
        "sequence",
        "solution_first_allowed",
        "arbitrary_tools_allowed",
        "checkpoint_or_resume_allowed",
    }
    assert not corpus["workflow_contract"]["solution_first_allowed"]
    assert not corpus["workflow_contract"]["arbitrary_tools_allowed"]
    assert not corpus["workflow_contract"]["checkpoint_or_resume_allowed"]

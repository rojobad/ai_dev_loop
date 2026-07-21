"""Shared fixtures for Phase 16.3 PR review v2 domain tests."""

from __future__ import annotations

import pytest
from tests.unit.pr_review_v2.helpers import HASH_2, SHA_A, T0, artifact

from ai_dev_loop.pr_review_v2.domain import (
    ExistingPrOrigin,
    PreparedState,
    PullRequestBinding,
    RepositoryIdentity,
    SourceRunOrigin,
    WorkflowLimits,
)


@pytest.fixture
def repo() -> RepositoryIdentity:
    return RepositoryIdentity(name_with_owner="acme/demo")


@pytest.fixture
def limits() -> WorkflowLimits:
    return WorkflowLimits(max_external_cycles=2, max_local_iterations=3)


@pytest.fixture
def binding(repo: RepositoryIdentity) -> PullRequestBinding:
    return PullRequestBinding(
        repository=repo,
        pr_number=42,
        head_branch="feature",
        base_branch="main",
        head_sha=SHA_A,
    )


@pytest.fixture
def source_origin(repo: RepositoryIdentity) -> SourceRunOrigin:
    return SourceRunOrigin(
        source_run_id="local-run-001",
        repository=repo,
        head_branch="feature",
        base_branch="main",
        expected_head_sha=SHA_A,
        accepted_patch=artifact("artifacts/accepted.patch"),
        execution_context_ref=artifact("artifacts/execution-context.json", HASH_2),
    )


@pytest.fixture
def existing_origin(binding: PullRequestBinding) -> ExistingPrOrigin:
    return ExistingPrOrigin(
        binding=binding,
        execution_context_ref=artifact("artifacts/execution-context.json", HASH_2),
    )


@pytest.fixture
def prepared_source(source_origin: SourceRunOrigin, limits: WorkflowLimits) -> PreparedState:
    return PreparedState(
        run_id="prv2-source-001",
        origin=source_origin,
        limits=limits,
        entered_at=T0,
    )


@pytest.fixture
def prepared_existing(existing_origin: ExistingPrOrigin, limits: WorkflowLimits) -> PreparedState:
    return PreparedState(
        run_id="prv2-existing-001",
        origin=existing_origin,
        limits=limits,
        entered_at=T0,
    )

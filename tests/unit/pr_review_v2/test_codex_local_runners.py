"""Unit tests for Codex local resume runners."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from ai_dev_loop.paths import schema_path
from ai_dev_loop.pr_review_v2.application.control_contracts import OriginKind
from ai_dev_loop.pr_review_v2.application.execution_context import (
    ExecutionContextArtifact,
    ExecutionContextCodex,
    ExecutionContextCursor,
    ExecutionContextPlanPrompt,
    ExecutionContextPrReviewV2,
    ExecutionContextRunBinding,
    ExecutionContextWorker,
    ExecutionContextWorkflow,
    ExternalAdjudicationDecision,
    ExternalAdjudicationResultArtifact,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    AdjudicationDecisionKind,
    ArtifactRef,
    PullRequestBinding,
    RepositoryIdentity,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    AdjudicateThreadsEffect,
    GeneratePublicationTextEffect,
)
from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import (
    EXTERNAL_ADJUDICATION_SCHEMA,
    PUBLICATION_GENERATION_SCHEMA,
    CodexLocalRunnerError,
    ExternalAdjudicationPayload,
    FakeCodexProcessRunner,
    PublicationTextRunner,
    ThreadAdjudicationRunner,
    build_codex_resume_argv,
)

RUN_ID = "prv2-codex-001"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
SHA_A = "a" * 40
HASH_1 = "1" * 64
HASH_2 = "2" * 64
THREAD_ID = "PRRT_thread_1"
CTX_HASH = "c" * 64


def _execution_context() -> ExecutionContextArtifact:
    return ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from=OriginKind.EXISTING_PR,
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
        ),
        cursor=ExecutionContextCursor(
            model="composer-2.5-fast",
            command="agent",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        codex=ExecutionContextCodex(
            session_id=SESSION_ID,
            review_model="gpt-5",
            review_reasoning_effort="high",
            command="codex",
            sandbox="workspace-write",
            review_skill="review-staged-cursor-execution",
            external_review_skill="review-github-pr-feedback",
        ),
        workflow=ExecutionContextWorkflow(
            max_local_iterations=3,
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
        ),
        pr_review_v2=ExecutionContextPrReviewV2(
            gh_command="gh",
            git_command="git",
            ssh_command="ssh",
            remote_name="origin",
            reviewer_logins=("chatgpt-codex-connector",),
            review_trigger_body="@codex review",
            user_mention="operator",
            poll_interval_seconds=60,
            max_external_cycles=8,
            per_call_timeout_seconds=60,
            overall_timeout_seconds=180,
            max_pages=20,
            max_items=500,
            max_server_directed_wait_seconds=3600,
            no_findings_enabled=False,
            worker=ExecutionContextWorker(
                lease_ttl_seconds=30,
                heartbeat_interval_seconds=10,
                idle_poll_seconds=1,
            ),
        ),
        plan_prompt=ExecutionContextPlanPrompt(
            plan_path="docs/plans/plan.md",
            plan_sha256=HASH_1,
            prompt_path="docs/plans/prompt.txt",
            prompt_sha256=HASH_2,
        ),
        repository_root="/tmp/repo",
    )


def _publication_effect() -> GeneratePublicationTextEffect:
    return GeneratePublicationTextEffect(
        effect_id="effect-publication-001",
        idempotency_key="idem-publication-001",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=3,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        evidence_ref=ArtifactRef(relative_path="local/evidence.json", sha256=HASH_1),
        patch_ref=ArtifactRef(relative_path="local/patch.patch", sha256=HASH_2),
    )


def _snapshot_bytes(*, thread_id: str = THREAD_ID) -> bytes:
    payload = {
        "head_sha": SHA_A,
        "cycle_number": 1,
        "eligible_threads": [{"thread_id": thread_id}],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _pr_binding() -> PullRequestBinding:
    return PullRequestBinding(
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        pr_number=1,
        head_sha=SHA_A,
        head_branch="feature",
        base_branch="main",
    )


def _adjudication_effect(snapshot: bytes) -> AdjudicateThreadsEffect:
    digest = hashlib.sha256(snapshot).hexdigest()
    return AdjudicateThreadsEffect(
        effect_id="effect-adjudication-001",
        idempotency_key="idem-adjudication-001",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=3,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        binding=_pr_binding(),
        frozen_thread_ids=(THREAD_ID,),
        snapshot_ref=ArtifactRef(relative_path="local/snapshot.json", sha256=digest),
        execution_context_ref=ArtifactRef(
            relative_path="local/execution-context.json", sha256=CTX_HASH
        ),
    )


def _load_packaged_schema(name: str) -> dict[str, Any]:
    path = schema_path(name)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _assert_strict_structured_output_contract(
    node: object,
    *,
    path: str,
) -> None:
    """Recursively enforce Codex strict output: every property key is required."""
    if isinstance(node, list):
        for index, item in enumerate(node):
            _assert_strict_structured_output_contract(item, path=f"{path}[{index}]")
        return
    if not isinstance(node, dict):
        return
    properties = node.get("properties")
    if isinstance(properties, dict):
        assert node.get("additionalProperties") is False, (
            f"{path}: closed Codex output objects must set additionalProperties=false"
        )
        required = node.get("required")
        assert isinstance(required, list), f"{path}: required must be a list"
        missing = set(properties) - set(required)
        extra = set(required) - set(properties)
        assert not missing and not extra, (
            f"{path}: required must include every key in properties; "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )
        for key, child in properties.items():
            _assert_strict_structured_output_contract(child, path=f"{path}.properties.{key}")
    for key, child in node.items():
        if key == "properties":
            continue
        _assert_strict_structured_output_contract(child, path=f"{path}.{key}")


def test_build_codex_resume_argv_contains_resume_and_session_id() -> None:
    argv = build_codex_resume_argv(
        codex_command="codex",
        repo_root="/tmp/repo",
        sandbox="workspace-write",
        session_id=SESSION_ID,
        review_model="gpt-5",
        review_reasoning_effort="high",
        schema_path_value=schema_path(PUBLICATION_GENERATION_SCHEMA),
        result_path=Path("/tmp/result.json"),
    )
    assert "resume" in argv
    assert SESSION_ID in argv
    assert "--last" not in argv


def test_codex_output_schemas_require_every_property_key() -> None:
    for name in (PUBLICATION_GENERATION_SCHEMA, EXTERNAL_ADJUDICATION_SCHEMA):
        schema = _load_packaged_schema(name)
        _assert_strict_structured_output_contract(schema, path=name)


def test_external_adjudication_schema_requires_nullable_keys() -> None:
    schema = _load_packaged_schema(EXTERNAL_ADJUDICATION_SCHEMA)
    assert set(schema["required"]) == {"decisions", "fix_prompt_text"}
    fix_prompt = schema["properties"]["fix_prompt_text"]
    assert fix_prompt["type"] == ["string", "null"]
    assert "description" in fix_prompt

    item = schema["properties"]["decisions"]["items"]
    assert set(item["required"]) == {
        "thread_id",
        "decision",
        "safe_summary",
        "reply_body",
    }
    assert item["properties"]["reply_body"]["type"] == ["string", "null"]
    assert "description" in item["properties"]["reply_body"]
    assert item["additionalProperties"] is False
    assert item["properties"]["decision"]["enum"] == [
        "actionable",
        "not_applicable",
        "uncertain",
    ]
    assert schema["additionalProperties"] is False


def test_external_adjudication_payload_shapes_pass_parser_and_domain() -> None:
    all_actionable = {
        "decisions": [
            {
                "thread_id": THREAD_ID,
                "decision": "actionable",
                "safe_summary": "needs a fix",
                "reply_body": None,
            }
        ],
        "fix_prompt_text": "Please fix the reported issue",
    }
    parsed_actionable = ExternalAdjudicationPayload.model_validate(all_actionable)
    assert parsed_actionable.decisions[0].reply_body is None
    assert parsed_actionable.fix_prompt_text is not None
    ExternalAdjudicationResultArtifact(
        decisions=(
            ExternalAdjudicationDecision(
                thread_id=THREAD_ID,
                decision=AdjudicationDecisionKind.ACTIONABLE,
                safe_summary="needs a fix",
                reply_body=None,
            ),
        ),
        fix_prompt_text="Please fix the reported issue",
        run_id=RUN_ID,
        cycle_number=1,
        effect_id="effect-adjudication-001",
        bound_head_sha=SHA_A,
        frozen_thread_ids=(THREAD_ID,),
        snapshot_ref_sha256=HASH_1,
        execution_context_ref_sha256=CTX_HASH,
    )

    reply_uncertain = {
        "decisions": [
            {
                "thread_id": THREAD_ID,
                "decision": "uncertain",
                "safe_summary": "needs clarification",
                "reply_body": "Please clarify the expected behavior",
            }
        ],
        "fix_prompt_text": None,
    }
    parsed_reply = ExternalAdjudicationPayload.model_validate(reply_uncertain)
    assert parsed_reply.fix_prompt_text is None
    assert parsed_reply.decisions[0].reply_body is not None
    ExternalAdjudicationResultArtifact(
        decisions=(
            ExternalAdjudicationDecision(
                thread_id=THREAD_ID,
                decision=AdjudicationDecisionKind.UNCERTAIN,
                safe_summary="needs clarification",
                reply_body="Please clarify the expected behavior",
            ),
        ),
        fix_prompt_text=None,
        run_id=RUN_ID,
        cycle_number=1,
        effect_id="effect-adjudication-001",
        bound_head_sha=SHA_A,
        frozen_thread_ids=(THREAD_ID,),
        snapshot_ref_sha256=HASH_1,
        execution_context_ref_sha256=CTX_HASH,
    )


def test_gate_b_actionable_with_reply_and_fix_prompt_fails_domain_closed() -> None:
    """Reproduce the sanitized Gate B contradiction: schema-valid, domain-invalid."""
    contradictory = {
        "decisions": [
            {
                "thread_id": THREAD_ID,
                "decision": "actionable",
                "safe_summary": "SENSITIVE_ADJUDICATION_SUMMARY_DO_NOT_LEAK",
                "reply_body": "SENSITIVE_REPLY_BODY_DO_NOT_LEAK",
            }
        ],
        "fix_prompt_text": "SENSITIVE_FIX_PROMPT_DO_NOT_LEAK",
    }
    parsed = ExternalAdjudicationPayload.model_validate(contradictory)
    assert parsed.decisions[0].reply_body is not None
    assert parsed.fix_prompt_text is not None
    with pytest.raises(Exception, match="all-actionable adjudication forbids reply_body"):
        ExternalAdjudicationResultArtifact(
            decisions=(
                ExternalAdjudicationDecision(
                    thread_id=THREAD_ID,
                    decision=AdjudicationDecisionKind.ACTIONABLE,
                    safe_summary="SENSITIVE_ADJUDICATION_SUMMARY_DO_NOT_LEAK",
                    reply_body="SENSITIVE_REPLY_BODY_DO_NOT_LEAK",
                ),
            ),
            fix_prompt_text="SENSITIVE_FIX_PROMPT_DO_NOT_LEAK",
            run_id=RUN_ID,
            cycle_number=1,
            effect_id="effect-adjudication-001",
            bound_head_sha=SHA_A,
            frozen_thread_ids=(THREAD_ID,),
            snapshot_ref_sha256=HASH_1,
            execution_context_ref_sha256=CTX_HASH,
        )


def test_adjudication_runner_converts_domain_invalid_to_privacy_safe_error(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_bytes()
    reply = "SENSITIVE_REPLY_BODY_DO_NOT_LEAK"
    summary = "SENSITIVE_ADJUDICATION_SUMMARY_DO_NOT_LEAK"
    fix_prompt = "SENSITIVE_FIX_PROMPT_DO_NOT_LEAK"
    fake = FakeCodexProcessRunner(
        result_payload={
            "decisions": [
                {
                    "thread_id": THREAD_ID,
                    "decision": "actionable",
                    "safe_summary": summary,
                    "reply_body": reply,
                }
            ],
            "fix_prompt_text": fix_prompt,
        }
    )
    runner = ThreadAdjudicationRunner(
        artifact_root=tmp_path / "art",
        process_runner=fake,
        timeout_seconds=30.0,
    )
    effect = _adjudication_effect(snapshot)
    with pytest.raises(CodexLocalRunnerError, match="failed domain validation") as raised:
        runner.adjudicate(
            run_id=RUN_ID,
            session_id=SESSION_ID,
            repo_root="/tmp/repo",
            execution_context=_execution_context(),
            effect=effect,
            snapshot_artifact_bytes_or_path=snapshot,
            frozen_thread_ids=(THREAD_ID,),
        )
    message = str(raised.value)
    assert message == "codex adjudication result failed domain validation"
    for needle in (reply, summary, fix_prompt, SESSION_ID, "forbids reply_body", "input_value"):
        assert needle not in message
    assert fake.last_stdin is not None
    assert 'decision == "actionable", reply_body must be null' in fake.last_stdin
    assert 'decision is "not_applicable" or "uncertain"' in fake.last_stdin
    assert "every decision is actionable, fix_prompt_text must be a non-empty string" in (
        fake.last_stdin
    )
    assert "any decision is non-actionable, fix_prompt_text must be null" in fake.last_stdin
    assert "exactly one decision per frozen thread" in fake.last_stdin
    assert "Return schema-constrained JSON only." in fake.last_stdin
    assert fake.last_argv is not None
    assert SESSION_ID in fake.last_argv
    assert "--last" not in fake.last_argv


def test_adjudication_runner_accepts_valid_all_actionable_and_non_actionable(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_bytes()
    effect = _adjudication_effect(snapshot)
    actionable_fake = FakeCodexProcessRunner(
        result_payload={
            "decisions": [
                {
                    "thread_id": THREAD_ID,
                    "decision": "actionable",
                    "safe_summary": "needs fix",
                    "reply_body": None,
                }
            ],
            "fix_prompt_text": "Please fix",
        }
    )
    actionable = ThreadAdjudicationRunner(
        artifact_root=tmp_path / "art-a",
        process_runner=actionable_fake,
        timeout_seconds=30.0,
    ).adjudicate(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        repo_root="/tmp/repo",
        execution_context=_execution_context(),
        effect=effect,
        snapshot_artifact_bytes_or_path=snapshot,
        frozen_thread_ids=(THREAD_ID,),
    )
    assert actionable.fix_prompt_text == "Please fix"
    assert actionable.decisions[0].reply_body is None

    reply_fake = FakeCodexProcessRunner(
        result_payload={
            "decisions": [
                {
                    "thread_id": THREAD_ID,
                    "decision": "not_applicable",
                    "safe_summary": "already addressed",
                    "reply_body": "Thanks, this was already fixed.",
                }
            ],
            "fix_prompt_text": None,
        }
    )
    replied = ThreadAdjudicationRunner(
        artifact_root=tmp_path / "art-b",
        process_runner=reply_fake,
        timeout_seconds=30.0,
    ).adjudicate(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        repo_root="/tmp/repo",
        execution_context=_execution_context(),
        effect=effect,
        snapshot_artifact_bytes_or_path=snapshot,
        frozen_thread_ids=(THREAD_ID,),
    )
    assert replied.fix_prompt_text is None
    assert replied.decisions[0].reply_body == "Thanks, this was already fixed."


def test_publication_runner_uses_resume_argv(tmp_path: Path) -> None:
    fake = FakeCodexProcessRunner(
        result_payload={
            "title": "Title",
            "body": "Body",
            "commit_subject": "Subject",
            "commit_body": "Commit body",
        }
    )
    runner = PublicationTextRunner(
        artifact_root=tmp_path / "art",
        process_runner=fake,
        timeout_seconds=30.0,
    )
    effect = _publication_effect()
    result = runner.generate(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        repo_root="/tmp/repo",
        execution_context=_execution_context(),
        evidence_ref=effect.evidence_ref,
        patch_ref=effect.patch_ref,
        effect=effect,
    )
    assert fake.last_argv is not None
    assert "resume" in fake.last_argv
    assert SESSION_ID in fake.last_argv
    assert "--last" not in fake.last_argv
    assert str(schema_path(PUBLICATION_GENERATION_SCHEMA)) in fake.last_argv
    assert result.title == "Title"
    assert result.commit_subject == "Subject"


def test_adjudication_runner_uses_external_schema_and_exact_session(tmp_path: Path) -> None:
    snapshot = _snapshot_bytes()
    fake = FakeCodexProcessRunner(
        result_payload={
            "decisions": [
                {
                    "thread_id": THREAD_ID,
                    "decision": "actionable",
                    "safe_summary": "needs fix",
                    "reply_body": None,
                }
            ],
            "fix_prompt_text": "Please fix",
        }
    )
    runner = ThreadAdjudicationRunner(
        artifact_root=tmp_path / "art",
        process_runner=fake,
        timeout_seconds=30.0,
    )
    effect = _adjudication_effect(snapshot)
    result = runner.adjudicate(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        repo_root="/tmp/repo",
        execution_context=_execution_context(),
        effect=effect,
        snapshot_artifact_bytes_or_path=snapshot,
        frozen_thread_ids=(THREAD_ID,),
    )
    assert fake.last_argv is not None
    assert "resume" in fake.last_argv
    assert SESSION_ID in fake.last_argv
    assert "--last" not in fake.last_argv
    assert str(schema_path(EXTERNAL_ADJUDICATION_SCHEMA)) in fake.last_argv
    assert "--output-schema" in fake.last_argv
    schema_index = fake.last_argv.index("--output-schema")
    assert fake.last_argv[schema_index + 1].endswith(EXTERNAL_ADJUDICATION_SCHEMA)
    assert result.fix_prompt_text == "Please fix"
    assert result.decisions[0].reply_body is None


def test_publication_runner_rejects_session_mismatch(tmp_path: Path) -> None:
    runner = PublicationTextRunner(
        artifact_root=tmp_path / "art",
        process_runner=FakeCodexProcessRunner(),
        timeout_seconds=30.0,
    )
    with pytest.raises(Exception, match="session identity mismatch"):
        runner.generate(
            run_id=RUN_ID,
            session_id="33333333-3333-4333-8333-333333333333",
            repo_root="/tmp/repo",
            execution_context=_execution_context(),
            evidence_ref=_publication_effect().evidence_ref,
            patch_ref=_publication_effect().patch_ref,
            effect=_publication_effect(),
        )

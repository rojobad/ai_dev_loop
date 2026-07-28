"""Phase 16.8 effect/crash contract matrix and production-boundary helpers."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from tests.integration.stateful_fake_gh import StatefulFakeGhController

from ai_dev_loop.pr_review_v2.application.contracts import Clock
from ai_dev_loop.pr_review_v2.application.execution_context import (
    ExecutionContextPrReviewV2,
    ExecutionContextWorker,
)
from ai_dev_loop.pr_review_v2.application.github_read import (
    GitHubReadPolicy,
    ObservationSnapshot,
)
from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef
from ai_dev_loop.pr_review_v2.domain.effects import (
    LOCAL_KINDS,
    MUTATING_KINDS,
    READ_ONLY_KINDS,
    RECONCILING_KINDS,
    ObserveBotReviewEffect,
    all_pr_review_effect_kinds,
)
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import (
    GhApiTransport,
    build_minimal_gh_env,
)
from ai_dev_loop.pr_review_v2.infrastructure.github_read_gateway import GitHubReadGateway
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import ReviewArtifactStore
from ai_dev_loop.pr_review_v2.runtime_factory import build_github_read_policy
from ai_dev_loop.pr_review_v2.workers.github_read_executor import GitHubReadExecutor

AuthorityClass = Literal[
    "READ", "LOCAL", "MUTATING", "RECONCILING", "CONTROL", "TIMER", "LEASE", "SUPERVISOR"
]

MUTATING_CRASH_WINDOWS = (
    "before_write",
    "ambiguous_apply_timeout",
    "apply_before_complete",
    "complete_before_worker_failure",
    "stale_authority",
    "lease_loss",
    "abort",
)

SHA_B = "b" * 40
MARKER = "pr-review:run-1:cycle:01:review-trigger"
SESSION = "11111111-1111-1111-1111-111111111111"


@dataclass(frozen=True, slots=True)
class EffectResiliencePolicy:
    effect_kind: str
    authority: AuthorityClass
    immutable_input_fields: tuple[str, ...]
    protected_output_fields: tuple[str, ...]
    idempotency_target: str
    retry_rule: str
    reconcile_rule: str
    lease_expiry: str
    abort_behavior: str
    crash_checkpoints: tuple[str, ...]


EFFECT_RESILIENCE_MATRIX: tuple[EffectResiliencePolicy, ...] = (
    EffectResiliencePolicy(
        effect_kind="observe_bot_review",
        authority="READ",
        immutable_input_fields=("binding", "bound_head_sha", "poll_sequence"),
        protected_output_fields=("observation_ref", "safe_summary"),
        idempotency_target="poll_sequence",
        retry_rule="transient backoff then same dispatch",
        reconcile_rule="read-only requeue on lease loss",
        lease_expiry="requeue same dispatch",
        abort_behavior="pause observation; no writes",
        crash_checkpoints=("before_transport", "after_artifact_before_complete_claim"),
    ),
    EffectResiliencePolicy(
        effect_kind="generate_publication_text",
        authority="LOCAL",
        immutable_input_fields=(
            "execution_context_ref",
            "patch_ref",
            "evidence_ref",
            "codex.session_id",
        ),
        protected_output_fields=("publication_text_ref", "protected_cache"),
        idempotency_target="effect-bound protected cache",
        retry_rule="bounded attempts then pause",
        reconcile_rule="verified cache replay only",
        lease_expiry="pause same LOCAL effect",
        abort_behavior="signal owned Codex child; fence late result",
        crash_checkpoints=("before_spawn", "mid_codex", "after_result_before_complete_claim"),
    ),
    EffectResiliencePolicy(
        effect_kind="adjudicate_threads",
        authority="LOCAL",
        immutable_input_fields=(
            "execution_context_ref",
            "observation_ref",
            "frozen_thread_set",
            "codex.session_id",
        ),
        protected_output_fields=("adjudication_ref", "fix_prompt_ref", "protected_cache"),
        idempotency_target="effect-bound protected cache",
        retry_rule="bounded attempts then pause",
        reconcile_rule="verified cache replay only",
        lease_expiry="pause same LOCAL effect",
        abort_behavior="signal owned Codex child; fence late result",
        crash_checkpoints=("before_spawn", "mid_codex", "after_result_before_complete_claim"),
    ),
    EffectResiliencePolicy(
        effect_kind="run_local_fix",
        authority="LOCAL",
        immutable_input_fields=(
            "execution_context_ref",
            "fix_prompt_ref",
            "carrier_run_id",
            "cursor.chat_id",
            "codex.session_id",
        ),
        protected_output_fields=("local_fix_result_ref", "carrier_state", "protected_cache"),
        idempotency_target="carrier + protected local result",
        retry_rule="bounded attempts then pause",
        reconcile_rule="terminal carrier replay without agent call",
        lease_expiry="pause same LOCAL effect",
        abort_behavior="carrier Cursor then owned Codex; fence late result",
        crash_checkpoints=(
            "before_carrier_seed",
            "mid_cursor",
            "after_staging_before_review",
            "after_result_before_complete_claim",
        ),
    ),
    EffectResiliencePolicy(
        effect_kind="commit_patch",
        authority="MUTATING",
        immutable_input_fields=("patch_ref", "message_ref", "expected_head_sha", "bound_head_sha"),
        protected_output_fields=("commit_sha", "new_head_sha", "write_evidence_ref"),
        idempotency_target="content-bound commit identity",
        retry_rule="reconcile then retry same logical effect",
        reconcile_rule="APPLIED|PROVEN_NOT_APPLIED|UNRESOLVED",
        lease_expiry="reconcile before retry write",
        abort_behavior="fence late completion; preserve repo",
        crash_checkpoints=("before_mutation", "apply_then_timeout", "after_apply_before_complete"),
    ),
    EffectResiliencePolicy(
        effect_kind="push_commit",
        authority="MUTATING",
        immutable_input_fields=("commit_sha", "remote_ref", "expected_remote_sha_before_push"),
        protected_output_fields=("remote_sha", "write_evidence_ref"),
        idempotency_target="commit_sha + remote ref",
        retry_rule="reconcile then retry same logical effect",
        reconcile_rule="APPLIED|PROVEN_NOT_APPLIED|UNRESOLVED",
        lease_expiry="reconcile before retry write",
        abort_behavior="fence late completion; preserve repo",
        crash_checkpoints=("before_mutation", "apply_then_timeout", "after_apply_before_complete"),
    ),
    EffectResiliencePolicy(
        effect_kind="create_or_update_pr",
        authority="MUTATING",
        immutable_input_fields=("publication_text_ref", "binding", "bound_head_sha"),
        protected_output_fields=("pr_binding", "content_bound_marker", "write_evidence_ref"),
        idempotency_target="content-bound PR body marker",
        retry_rule="reconcile then retry same logical effect",
        reconcile_rule="APPLIED|PROVEN_NOT_APPLIED|UNRESOLVED",
        lease_expiry="reconcile before retry write",
        abort_behavior="fence late completion; preserve remote evidence",
        crash_checkpoints=("before_mutation", "apply_then_timeout", "after_apply_before_complete"),
    ),
    EffectResiliencePolicy(
        effect_kind="request_bot_review",
        authority="MUTATING",
        immutable_input_fields=("binding", "marker", "bound_head_sha"),
        protected_output_fields=("trigger_evidence_ref", "comment_ref"),
        idempotency_target="opaque trigger marker",
        retry_rule="reconcile then retry same logical effect",
        reconcile_rule="APPLIED|PROVEN_NOT_APPLIED|UNRESOLVED",
        lease_expiry="reconcile before retry write",
        abort_behavior="fence late completion; preserve remote evidence",
        crash_checkpoints=("before_mutation", "apply_then_timeout", "after_apply_before_complete"),
    ),
    EffectResiliencePolicy(
        effect_kind="post_thread_reply",
        authority="MUTATING",
        immutable_input_fields=("thread_id", "reply_ref", "bound_head_sha"),
        protected_output_fields=("reply_ref", "content_bound_marker", "write_evidence_ref"),
        idempotency_target="thread + reply body hash",
        retry_rule="reconcile then retry same logical effect",
        reconcile_rule="APPLIED|PROVEN_NOT_APPLIED|UNRESOLVED",
        lease_expiry="reconcile before retry write",
        abort_behavior="fence late completion; preserve remote evidence",
        crash_checkpoints=("before_mutation", "apply_then_timeout", "after_apply_before_complete"),
    ),
    EffectResiliencePolicy(
        effect_kind="update_pr_text",
        authority="MUTATING",
        immutable_input_fields=("publication_text_ref", "binding", "bound_head_sha"),
        protected_output_fields=(
            "publication_text_ref",
            "content_bound_marker",
            "write_evidence_ref",
        ),
        idempotency_target="content-bound PR text marker",
        retry_rule="reconcile then retry same logical effect",
        reconcile_rule="APPLIED|PROVEN_NOT_APPLIED|UNRESOLVED",
        lease_expiry="reconcile before retry write",
        abort_behavior="fence late completion; preserve remote evidence",
        crash_checkpoints=("before_mutation", "apply_then_timeout", "after_apply_before_complete"),
    ),
    EffectResiliencePolicy(
        effect_kind="resolve_thread",
        authority="MUTATING",
        immutable_input_fields=("thread_id", "binding", "bound_head_sha"),
        protected_output_fields=("thread_id", "write_evidence_ref"),
        idempotency_target="thread_id",
        retry_rule="reconcile then retry same logical effect",
        reconcile_rule="APPLIED|PROVEN_NOT_APPLIED|UNRESOLVED",
        lease_expiry="reconcile before retry write",
        abort_behavior="fence late completion; preserve remote evidence",
        crash_checkpoints=("before_mutation", "apply_then_timeout", "after_apply_before_complete"),
    ),
    EffectResiliencePolicy(
        effect_kind="reconcile_write",
        authority="RECONCILING",
        immutable_input_fields=("source_effect_id", "source_effect_kind", "idempotency_key"),
        protected_output_fields=("reconciliation_proof", "confirmed_outcome"),
        idempotency_target="source mutating effect id",
        retry_rule="bounded reconcile reads",
        reconcile_rule="never blind write",
        lease_expiry="requeue same reconcile dispatch",
        abort_behavior="pause; preserve ambiguous evidence",
        crash_checkpoints=("before_reconcile_read", "after_proof_before_complete"),
    ),
)


def matrix_effect_kinds() -> frozenset[str]:
    return frozenset(item.effect_kind for item in EFFECT_RESILIENCE_MATRIX)


def domain_effect_kinds() -> frozenset[str]:
    return all_pr_review_effect_kinds()


def assert_matrix_covers_all_effects() -> None:
    missing = domain_effect_kinds() - matrix_effect_kinds()
    extra = matrix_effect_kinds() - domain_effect_kinds()
    assert not missing, f"missing matrix entries for effects: {sorted(missing)}"
    assert not extra, f"unknown matrix entries: {sorted(extra)}"


def policy_for_kind(effect_kind: str) -> EffectResiliencePolicy:
    for item in EFFECT_RESILIENCE_MATRIX:
        if item.effect_kind == effect_kind:
            return item
    raise KeyError(effect_kind)


def authority_for_kind(kind: str) -> AuthorityClass:
    if kind in READ_ONLY_KINDS:
        return "READ"
    if kind in LOCAL_KINDS:
        return "LOCAL"
    if kind in RECONCILING_KINDS:
        return "RECONCILING"
    if kind in MUTATING_KINDS:
        return "MUTATING"
    raise KeyError(kind)


def assert_classifications_align() -> None:
    for policy in EFFECT_RESILIENCE_MATRIX:
        assert policy.authority == authority_for_kind(policy.effect_kind)


def execution_context_v2(
    *,
    no_findings_enabled: bool = False,
    no_findings_prefixes: tuple[str, ...] = (),
    accept_bot_thumbs_up: bool = False,
) -> ExecutionContextPrReviewV2:
    return ExecutionContextPrReviewV2(
        gh_command="gh",
        git_command="git",
        ssh_command="ssh",
        remote_name="origin",
        reviewer_logins=("chatgpt-codex-connector",),
        review_trigger_body="@codex review",
        user_mention="rojobad",
        poll_interval_seconds=60,
        max_external_cycles=8,
        per_call_timeout_seconds=60,
        overall_timeout_seconds=180,
        max_pages=20,
        max_items=500,
        max_server_directed_wait_seconds=3600,
        no_findings_enabled=no_findings_enabled,
        no_findings_prefixes=no_findings_prefixes,
        accept_bot_thumbs_up=accept_bot_thumbs_up,
        no_findings_prefix_length=12,
        worker=ExecutionContextWorker(
            lease_ttl_seconds=30,
            heartbeat_interval_seconds=10,
            idle_poll_seconds=1,
        ),
    )


def assemble_read_stack(
    tmp_path: Path,
    controller: StatefulFakeGhController,
    *,
    policy: GitHubReadPolicy,
    repo_cwd: str | None = None,
    clock: Clock | None = None,
) -> tuple[GitHubReadGateway, GitHubReadExecutor]:
    gh_path = controller.install(tmp_path / "bin")
    cwd = repo_cwd or str(tmp_path / "repo")
    Path(cwd).mkdir(parents=True, exist_ok=True)
    env = build_minimal_gh_env()
    env["PATH"] = f"{gh_path.parent}:{env.get('PATH', '')}"
    transport = GhApiTransport(
        command=str(gh_path),
        cwd=cwd,
        per_call_timeout_seconds=policy.per_call_timeout_seconds,
        env=env,
    )
    artifacts = ReviewArtifactStore(read_artifact_root(tmp_path))
    gateway = GitHubReadGateway(policy=policy, transport=transport, artifacts=artifacts)
    from tests.unit.pr_review_v2.durable_helpers import FakeClock

    executor = GitHubReadExecutor(
        gateway=gateway,
        policy=policy,
        clock=clock or FakeClock(datetime(2026, 7, 21, 12, 10, tzinfo=UTC)),
    )
    return gateway, executor


def read_artifact_root(tmp_path: Path) -> Path:
    """Artifact root used by every read stack assembled from ``tmp_path``."""

    return tmp_path / "artifacts"


class RecordingReadGateway:
    """Delegate to the production read gateway and record the refs it returns.

    The executor discards the observation ref for waiting outcomes, so recovery
    tests need the exact ``ArtifactRef`` the gateway handed to the executor.
    """

    def __init__(self, gateway: GitHubReadGateway) -> None:
        self._gateway = gateway
        self.observations: list[tuple[ObservationSnapshot, ArtifactRef]] = []

    def observe_with_artifact(
        self,
        effect: ObserveBotReviewEffect,
        *,
        observation_time: datetime,
    ) -> tuple[ObservationSnapshot, ArtifactRef]:
        result = self._gateway.observe_with_artifact(effect, observation_time=observation_time)
        self.observations.append(result)
        return result


def assemble_recording_read_stack(
    tmp_path: Path,
    controller: StatefulFakeGhController,
    *,
    policy: GitHubReadPolicy,
    clock: Clock,
    repo_cwd: str | None = None,
) -> tuple[RecordingReadGateway, GitHubReadExecutor]:
    gateway, _ = assemble_read_stack(
        tmp_path,
        controller,
        policy=policy,
        repo_cwd=repo_cwd,
        clock=clock,
    )
    recorder = RecordingReadGateway(gateway)
    executor = GitHubReadExecutor(gateway=recorder, policy=policy, clock=clock)
    return recorder, executor


def seed_observation_for_effect(
    controller: StatefulFakeGhController,
    effect: ObserveBotReviewEffect,
) -> None:
    """Align the stateful fake ``gh`` fixture with the exact claimed observe effect."""

    assert effect.trigger_marker is not None
    owner, name = effect.binding.repository.name_with_owner.split("/", 1)
    controller.seed_waiting_observation(
        marker=effect.trigger_marker,
        head_sha=effect.bound_head_sha,
        owner=owner,
        name=name,
        pr_number=effect.binding.pr_number,
        head_branch=effect.binding.head_branch,
        base_branch=effect.binding.base_branch,
    )


def init_git_remote(tmp_path: Path) -> tuple[Path, Path, str]:
    work = tmp_path / "work"
    bare = tmp_path / "remote.git"
    work.mkdir()
    subprocess.check_call(["git", "init"], cwd=work)
    subprocess.check_call(["git", "config", "user.email", "p168@example.com"], cwd=work)
    subprocess.check_call(["git", "config", "user.name", "p168"], cwd=work)
    (work / "file.txt").write_text("base\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "file.txt"], cwd=work)
    subprocess.check_call(["git", "commit", "-m", "init"], cwd=work, stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "checkout", "-b", "feature"], cwd=work)
    parent = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()
    subprocess.check_call(["git", "init", "--bare", str(bare)], cwd=tmp_path)
    subprocess.check_call(["git", "remote", "add", "origin", str(bare)], cwd=work)
    subprocess.check_call(["git", "push", "-u", "origin", "feature"], cwd=work)
    return work, bare, parent


def scan_text_surfaces(*paths: Path, forbidden: tuple[str, ...]) -> None:
    for path in paths:
        if not path.exists():
            continue
        targets = [path] if path.is_file() else list(path.rglob("*"))
        for target in targets:
            if not target.is_file():
                continue
            try:
                text = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for needle in forbidden:
                assert needle not in text, f"{needle!r} leaked in {target}"


def sqlite_dump(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        chunks: list[str] = []
        for (name,) in rows:
            data = conn.execute(f"SELECT * FROM {name}").fetchall()
            chunks.append(json.dumps(data, default=str))
        return "\n".join(chunks)
    finally:
        conn.close()


def assert_runtime_read_policy_disabled_ignores_frozen_rules() -> None:
    v2 = execution_context_v2(
        no_findings_enabled=False,
        no_findings_prefixes=("No findings found.",),
        accept_bot_thumbs_up=True,
    )
    policy = build_github_read_policy(v2, repository_cwd="/tmp/repo")
    assert policy.accepted_no_findings_prefixes == ()
    assert policy.accept_bot_thumbs_up is False
    assert policy.no_findings_enabled is False
    assert policy.comment_no_findings_enabled is False


def assert_runtime_read_policy_enabled_preserves_rules() -> None:
    v2 = execution_context_v2(
        no_findings_enabled=True,
        no_findings_prefixes=("No findings",),
        accept_bot_thumbs_up=True,
    )
    policy = build_github_read_policy(v2, repository_cwd="/tmp/repo")
    assert policy.accepted_no_findings_prefixes == ("No findings",)
    assert policy.accept_bot_thumbs_up is True
    assert policy.no_findings_enabled is True

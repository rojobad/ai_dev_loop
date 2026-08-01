"""Phase 16.7 correction round-4 regressions (findings 1-4)."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.helpers import HASH_1, HASH_2, SHA_A

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.pr_review_v2.application.execution_context import (
    ExecutionContextArtifact,
    ExecutionContextCodex,
    ExecutionContextCursor,
    ExecutionContextPlanPrompt,
    ExecutionContextPrReviewV2,
    ExecutionContextRunBinding,
    ExecutionContextWorker,
    ExecutionContextWorkflow,
)
from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef, RepositoryIdentity
from ai_dev_loop.pr_review_v2.domain.effects import (
    AdjudicateThreadsEffect,
    GeneratePublicationTextEffect,
)
from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import (
    CodexLocalRunnerError,
    FakeCodexProcessRunner,
    PublicationTextRunner,
    ThreadAdjudicationRunner,
)
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import (
    CarrierSeed,
    LocalFixAdapterError,
    carrier_run_id,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
    LOCAL_FIX_RESULT_DIR,
    ProtectedResultStore,
)
from ai_dev_loop.pr_review_v2.runtime_factory import ProcessCodexRunner
from ai_dev_loop.pr_review_v2.workers.owned_children import OwnedChildStore
from ai_dev_loop.pr_review_v2_carrier import FilesystemLocalCarrierRuntime
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import RunStatus, save_run_state, sha256_bytes

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
SESSION = "11111111-1111-1111-1111-111111111111"
RUN_ID = "run-corr4-1"
PLAN_BYTES = b"frozen-plan-for-corr4\n"
PROMPT_BYTES = b"frozen-prompt-for-corr4\n"
PLAN_SHA = sha256_bytes(PLAN_BYTES)
PROMPT_SHA = sha256_bytes(PROMPT_BYTES)
FIX_BYTES = b"fix-prompt-body\n"
THREAD_ID = "PRRT_thread_1"


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.check_call(["git", "init", "-b", "main"], cwd=repo, stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "config", "user.email", "t@example.com"], cwd=repo)
    subprocess.check_call(["git", "config", "user.name", "t"], cwd=repo)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "plans").mkdir()
    (repo / "plans" / "x.md").write_bytes(PLAN_BYTES)
    (repo / "prompts").mkdir()
    (repo / "prompts" / "prompt.txt").write_bytes(PROMPT_BYTES)
    subprocess.check_call(["git", "add", "app.py", "plans", "prompts"], cwd=repo)
    subprocess.check_call(["git", "commit", "-m", "init"], cwd=repo, stdout=subprocess.DEVNULL)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    return repo, head.lower()


def _seed(repo: Path, head: str, *, chat_id: str | None = "chat-1") -> CarrierSeed:
    return CarrierSeed(
        v2_run_id=RUN_ID,
        cycle_number=1,
        effect_id="eff-1",
        repository_root=str(repo.resolve()),
        plan_path="plans/x.md",
        prompt_path="prompts/prompt.txt",
        plan_sha256=PLAN_SHA,
        prompt_sha256=PROMPT_SHA,
        plan_bytes=PLAN_BYTES,
        prompt_bytes=PROMPT_BYTES,
        cursor_chat_id=chat_id,
        cursor_model="composer-2.5-fast",
        cursor_command="agent",
        cursor_output_format="stream-json",
        cursor_force=True,
        cursor_trust_workspace=True,
        cursor_sandbox="disabled",
        codex_session_id=SESSION,
        codex_command="codex",
        review_model="gpt-5.5",
        review_reasoning_effort="medium",
        review_skill="review-staged-cursor-execution",
        codex_sandbox="workspace-write",
        max_local_iterations=3,
        cursor_timeout_minutes=30,
        codex_timeout_minutes=30,
        bound_head_sha=head,
        expected_branch="main",
        fix_prompt_bytes=FIX_BYTES,
    )


def _ctx(repo_root: str) -> ExecutionContextArtifact:
    return ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from="source_run",
            source_run_id="src-1",
            repository="acme/demo",
            head_branch="main",
            base_branch="main",
            expected_head_sha=SHA_A,
        ),
        cursor=ExecutionContextCursor(
            chat_id="chat-1",
            model="composer-2.5-fast",
            command="agent",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        codex=ExecutionContextCodex(
            session_id=SESSION,
            review_model="gpt-5.5",
            review_reasoning_effort="medium",
            command="codex",
            sandbox="workspace-write",
            review_skill="review-staged-cursor-execution",
            external_review_skill="review-github-pr-feedback",
        ),
        workflow=ExecutionContextWorkflow(
            max_local_iterations=3,
            cursor_timeout_minutes=30,
            codex_timeout_minutes=30,
        ),
        pr_review_v2=ExecutionContextPrReviewV2(
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
            no_findings_enabled=False,
            worker=ExecutionContextWorker(
                lease_ttl_seconds=30,
                heartbeat_interval_seconds=10,
                idle_poll_seconds=1,
            ),
        ),
        plan_prompt=ExecutionContextPlanPrompt(
            plan_path="plans/x.md",
            plan_sha256=PLAN_SHA,
            prompt_path="prompts/prompt.txt",
            prompt_sha256=PROMPT_SHA,
            accepted_patch_sha256=HASH_1,
        ),
        repository_root=repo_root,
    )


def test_process_codex_runner_timeout_covers_unread_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Child never reads stdin + payload larger than pipe capacity → timeout + reap."""

    runner = ProcessCodexRunner(
        artifact_root=tmp_path / "artifacts",
        run_id=RUN_ID,
        term_grace_seconds=0.05,
    )
    # ~1 MiB exceeds typical pipe capacity (~64 KiB) so a blocking write would hang.
    huge = "x" * (1024 * 1024)
    result = runner.run(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdin_text=huge,
        timeout_seconds=0.4,
    )
    assert result.timed_out is True
    children = OwnedChildStore(tmp_path / "artifacts")
    assert children.read(RUN_ID, "codex") is None


def test_carrier_binding_drift_categories(tmp_path: Path, isolated_xdg) -> None:
    repo, head = _git_repo(tmp_path)
    runtime = FilesystemLocalCarrierRuntime()
    seed = _seed(repo, head)
    cid = carrier_run_id(RUN_ID, 1, "eff-1")
    runtime.ensure_seeded_carrier(carrier_run_id=cid, seed=seed)
    path, state = load_run(cid)
    chat_payload = json.loads((path / "cursor" / "chat.json").read_text(encoding="utf-8"))
    assert chat_payload["chat_id"] == seed.cursor_chat_id
    assert chat_payload["provenance"] == "inherited_carrier_seed"

    # Codex session drift
    state.codex.session_id = "22222222-2222-2222-2222-222222222222"
    save_run_state(path, state)
    with pytest.raises(ValidationError, match="session"):
        runtime.verify_carrier_bindings(carrier_run_id=cid, seed=seed)

    # Restore and check Cursor sandbox drift
    state.codex.session_id = SESSION
    state.cursor.sandbox = "enabled"
    save_run_state(path, state)
    with pytest.raises(ValidationError, match="sandbox"):
        runtime.verify_carrier_bindings(carrier_run_id=cid, seed=seed)

    # Restore and plan bytes drift
    state.cursor.sandbox = "disabled"
    save_run_state(path, state)
    (path / "plan" / "plan.md").write_bytes(b"drifted-plan\n")
    with pytest.raises(ValidationError, match="plan"):
        runtime.verify_carrier_bindings(carrier_run_id=cid, seed=seed)

    # Restore plan and workflow timeout drift
    (path / "plan" / "plan.md").write_bytes(PLAN_BYTES)
    state.workflow.cursor_timeout_minutes = 99
    save_run_state(path, state)
    with pytest.raises(ValidationError, match="cursor_timeout"):
        runtime.verify_carrier_bindings(carrier_run_id=cid, seed=seed)

    # Expected chat when non-null must match
    state.workflow.cursor_timeout_minutes = 30
    state.cursor.chat_id = "other-chat"
    save_run_state(path, state)
    with pytest.raises(ValidationError, match="chat"):
        runtime.verify_carrier_bindings(carrier_run_id=cid, seed=seed)


def test_existing_pr_null_chat_to_created_chat_is_allowed(tmp_path: Path, isolated_xdg) -> None:
    repo, head = _git_repo(tmp_path)
    runtime = FilesystemLocalCarrierRuntime()
    seed = _seed(repo, head, chat_id=None)
    cid = carrier_run_id(RUN_ID, 1, "eff-null-chat")
    runtime.ensure_seeded_carrier(carrier_run_id=cid, seed=seed)
    path, state = load_run(cid)
    assert state.cursor.chat_id is None
    # Cursor creates a chat during the first cycle — seed remains null.
    state.cursor.chat_id = "chat-created-by-cursor"
    save_run_state(path, state)
    runtime.verify_carrier_bindings(carrier_run_id=cid, seed=seed)


def _accepted_review_payload(*, residual: bool = False) -> dict[str, object]:
    return {
        "has_actionable_findings": False,
        "findings_count": 0,
        "highest_severity": None,
        "cursor_fix_prompt": None,
        "tests_status": "failed" if residual else "passed",
        "review_markdown": "ok",
        "summary": "no findings",
    }


def _seed_terminal_carrier(
    tmp_path: Path,
    *,
    review_payload: dict[str, object] | None = None,
    result_sha: str | None = "auto",
    symlink_result: bool = False,
    omit_codex: bool = False,
    omit_result_path: bool = False,
    omit_result_sha: bool = False,
    residual_status: bool = False,
    staged_diff_path: str | None = None,
    omit_patch_sha: bool = False,
    patch_sha: str | None = "auto",
    patch_mode: int | None = 0o600,
) -> tuple[str, Path, CarrierSeed]:
    from ai_dev_loop.paths import set_sensitive_file_mode

    repo, head = _git_repo(tmp_path)
    runtime = FilesystemLocalCarrierRuntime()
    seed = _seed(repo, head)
    cid = carrier_run_id(RUN_ID, 1, "eff-term")
    runtime.ensure_seeded_carrier(carrier_run_id=cid, seed=seed)
    path, state = load_run(cid)
    reviews = path / "codex" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    result_rel = "codex/reviews/01.json"
    result_path = path / result_rel
    payload = review_payload if review_payload is not None else _accepted_review_payload()
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if symlink_result:
        target = tmp_path / "outside.json"
        target.write_bytes(raw)
        result_path.symlink_to(target)
    else:
        result_path.write_bytes(raw)
    digest = sha256_bytes(raw)
    recorded = digest if result_sha == "auto" else result_sha
    patch_rel = staged_diff_path if staged_diff_path is not None else "git/diffs/01.patch"
    patch_bytes = b"diff --git a/x b/x\n+fixed\n"
    if not patch_rel.startswith("/") and ".." not in patch_rel.split("/"):
        (path / "git" / "diffs").mkdir(parents=True, exist_ok=True)
        patch_file = path / patch_rel
        patch_file.parent.mkdir(parents=True, exist_ok=True)
        patch_file.write_bytes(patch_bytes)
        if patch_mode is not None:
            patch_file.chmod(patch_mode)
        else:
            set_sensitive_file_mode(patch_file)
    codex_section: dict[str, object] = {
        "events_path": "codex/events/01.jsonl",
        "stderr_path": "codex/events/01.stderr.txt",
        "report_path": "codex/reviews/01.md",
        "metadata_path": "codex/reviews/01.metadata.json",
        "exit_code": 0,
    }
    if not omit_result_path:
        codex_section["result_path"] = result_rel
    if not omit_result_sha and recorded is not None:
        codex_section["result_sha256"] = recorded
    git_section: dict[str, object] = {"staged_diff_path": patch_rel}
    patch_digest = sha256_bytes(patch_bytes)
    recorded_patch = patch_digest if patch_sha == "auto" else patch_sha
    if not omit_patch_sha and recorded_patch is not None:
        git_section["staged_diff_sha256"] = recorded_patch
    iteration: dict[str, object] = {
        "number": 1,
        "kind": "initial_implementation",
        "git": git_section,
    }
    if not omit_codex:
        iteration["codex"] = codex_section
    state.iterations = [iteration]
    state.cursor.chat_id = "chat-1"
    state.status = (
        RunStatus.COMPLETED_WITH_RESIDUAL_RISK if residual_status else RunStatus.COMPLETED
    )
    save_run_state(path, state)
    return cid, path, seed


def test_terminal_recovery_requires_complete_hash_verified_review(
    tmp_path: Path, isolated_xdg
) -> None:
    runtime = FilesystemLocalCarrierRuntime()
    cid, _path, seed = _seed_terminal_carrier(tmp_path)
    runtime.verify_carrier_bindings(carrier_run_id=cid, seed=seed)
    accepted = runtime.read_terminal_acceptance(cid, expected_session_id=SESSION)
    assert accepted is not None
    assert accepted.residual_risk is False


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"omit_codex": True}, "codex"),
        ({"omit_result_path": True}, "result path"),
        ({"omit_result_sha": True}, "result hash"),
        ({"result_sha": "0" * 64}, "hash mismatch"),
        ({"symlink_result": True}, "unsafe|symlink"),
        (
            {
                "review_payload": {
                    "has_actionable_findings": True,
                    "findings_count": 1,
                    "highest_severity": "P1",
                    "cursor_fix_prompt": "fix it",
                    "tests_status": "skipped_findings_present",
                    "review_markdown": "findings",
                    "summary": "findings",
                }
            },
            "actionable",
        ),
        ({"review_payload": {"not": "a review"}}, "canonical parse"),
    ],
)
def test_terminal_recovery_blocks_incomplete_or_bad_review(
    tmp_path: Path, isolated_xdg, kwargs: dict, match: str
) -> None:
    runtime = FilesystemLocalCarrierRuntime()
    cid, _path, _seed = _seed_terminal_carrier(tmp_path, **kwargs)
    with pytest.raises(ValidationError, match=match):
        runtime.read_terminal_acceptance(cid, expected_session_id=SESSION)


def test_terminal_recovery_blocks_missing_result_file(tmp_path: Path, isolated_xdg) -> None:
    runtime = FilesystemLocalCarrierRuntime()
    cid, path, _seed = _seed_terminal_carrier(tmp_path)
    (path / "codex" / "reviews" / "01.json").unlink()
    with pytest.raises(ValidationError, match="missing"):
        runtime.read_terminal_acceptance(cid, expected_session_id=SESSION)


def test_publication_and_adjudication_reject_extra_keys_and_wrong_types(
    tmp_path: Path,
) -> None:
    ctx = _ctx(repo_root=str(tmp_path))
    pub = PublicationTextRunner(
        artifact_root=tmp_path / "arts",
        process_runner=FakeCodexProcessRunner(
            result_payload={
                "title": "T",
                "body": "B",
                "commit_subject": "S",
                "commit_body": "C",
                "extra": "nope",
            }
        ),
        timeout_seconds=5,
    )
    effect = GeneratePublicationTextEffect(
        effect_id="pr-review:run-corr4-1:cycle:01:generate_publication_text",
        idempotency_key="pr-review:run-corr4-1:cycle:01:generate_publication_text",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        evidence_ref=ArtifactRef(relative_path="e.json", sha256=HASH_1),
        patch_ref=ArtifactRef(relative_path="p.patch", sha256=HASH_2),
    )
    with pytest.raises(CodexLocalRunnerError, match="schema validation"):
        pub.generate(
            run_id=RUN_ID,
            session_id=SESSION,
            repo_root=str(tmp_path),
            execution_context=ctx,
            evidence_ref=effect.evidence_ref,
            patch_ref=effect.patch_ref,
            effect=effect,
        )

    pub_wrong_type = PublicationTextRunner(
        artifact_root=tmp_path / "arts2",
        process_runner=FakeCodexProcessRunner(
            result_payload={
                "title": 123,
                "body": "B",
                "commit_subject": "S",
                "commit_body": "C",
            }
        ),
        timeout_seconds=5,
    )
    with pytest.raises(CodexLocalRunnerError, match="schema validation"):
        pub_wrong_type.generate(
            run_id=RUN_ID,
            session_id=SESSION,
            repo_root=str(tmp_path),
            execution_context=ctx,
            evidence_ref=effect.evidence_ref,
            patch_ref=effect.patch_ref,
            effect=effect,
        )

    adj = ThreadAdjudicationRunner(
        artifact_root=tmp_path / "arts3",
        process_runner=FakeCodexProcessRunner(
            result_payload={
                "decisions": [
                    {
                        "thread_id": THREAD_ID,
                        "decision": "actionable",
                        "safe_summary": "ok",
                        "reply_body": None,
                        "extra": True,
                    }
                ],
                "fix_prompt_text": "fix",
            }
        ),
        timeout_seconds=5,
    )
    snap = (
        b'{"eligible_threads":[{"thread_id":"PRRT_thread_1"}],"head_sha":"'
        + SHA_A.encode()
        + b'","cycle_number":1}'
    )
    snap_ref = ArtifactRef(relative_path="snap.json", sha256=sha256_bytes(snap))
    adj_effect = AdjudicateThreadsEffect(
        effect_id="pr-review:run-corr4-1:cycle:01:adjudicate_threads",
        idempotency_key="pr-review:run-corr4-1:cycle:01:adjudicate_threads",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        binding=__import__(
            "ai_dev_loop.pr_review_v2.domain.common", fromlist=["PullRequestBinding"]
        ).PullRequestBinding(
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            pr_number=1,
            head_branch="feature",
            base_branch="main",
            head_sha=SHA_A,
        ),
        frozen_thread_ids=(THREAD_ID,),
        snapshot_ref=snap_ref,
        execution_context_ref=ArtifactRef(relative_path="ctx.json", sha256=HASH_2),
    )
    with pytest.raises(CodexLocalRunnerError, match="schema validation"):
        adj.adjudicate(
            run_id=RUN_ID,
            session_id=SESSION,
            repo_root=str(tmp_path),
            execution_context=ctx,
            effect=adj_effect,
            snapshot_artifact_bytes_or_path=snap,
            frozen_thread_ids=(THREAD_ID,),
        )

    adj_wrong = ThreadAdjudicationRunner(
        artifact_root=tmp_path / "arts4",
        process_runner=FakeCodexProcessRunner(
            result_payload={
                "decisions": [
                    {
                        "thread_id": THREAD_ID,
                        "decision": "actionable",
                        "safe_summary": ["not", "a", "string"],
                        "reply_body": None,
                    }
                ],
                "fix_prompt_text": "fix",
            }
        ),
        timeout_seconds=5,
    )
    with pytest.raises(CodexLocalRunnerError, match="schema validation"):
        adj_wrong.adjudicate(
            run_id=RUN_ID,
            session_id=SESSION,
            repo_root=str(tmp_path),
            execution_context=ctx,
            effect=adj_effect,
            snapshot_artifact_bytes_or_path=snap,
            frozen_thread_ids=(THREAD_ID,),
        )


def test_terminal_crash_window_does_not_persist_when_review_invalid(
    tmp_path: Path,
    fake_clis,
    isolated_xdg,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid terminal review evidence must not create an accepted v2 local-fix result."""

    from ai_dev_loop.pr_review_v2.domain.common import LocalFixOutcomeKind, PullRequestBinding
    from ai_dev_loop.pr_review_v2.domain.effects import RunLocalFixEffect
    from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import LocalFixAdapter
    from ai_dev_loop.pr_review_v2.infrastructure.paths import run_artifact_root

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)

    repo, head = _git_repo(tmp_path)
    store = ProtectedResultStore(tmp_path / "artifacts")
    ctx = _ctx(repo_root=str(repo.resolve()))
    ctx = ctx.model_copy(
        update={
            "run_binding": ctx.run_binding.model_copy(
                update={"expected_head_sha": head, "head_branch": "main"}
            )
        }
    )
    ctx_ref = store.persist_execution_context(run_id=RUN_ID, artifact=ctx)
    store.persist_source_plan_bytes(run_id=RUN_ID, data=PLAN_BYTES)
    store.persist_source_prompt_bytes(run_id=RUN_ID, data=PROMPT_BYTES)
    fix_ref = store.persist_fix_prompt(run_id=RUN_ID, text=FIX_BYTES.decode())
    effect = RunLocalFixEffect(
        effect_id="pr-review:run-corr4-1:cycle:01:run_local_fix",
        idempotency_key="pr-review:run-corr4-1:cycle:01:run_local_fix",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=head,
        binding=PullRequestBinding(
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            pr_number=1,
            head_branch="main",
            base_branch="main",
            head_sha=head,
        ),
        actionable_thread_ids=(THREAD_ID,),
        fix_prompt_ref=fix_ref,
        execution_context_ref=ctx_ref,
    )
    adapter = LocalFixAdapter(runtime=FilesystemLocalCarrierRuntime(), store=store)
    first = adapter.execute(
        run_id=RUN_ID,
        effect=effect,
        execution_context=ctx,
        fix_prompt_bytes=FIX_BYTES,
        plan_bytes=PLAN_BYTES,
        prompt_bytes=PROMPT_BYTES,
    )
    assert first.outcome is LocalFixOutcomeKind.ACCEPTED
    cid = carrier_run_id(RUN_ID, 1, effect.effect_id)
    path, state = load_run(cid)
    # Corrupt recorded hash after successful terminalization.
    latest = dict(state.iterations[0])
    codex = dict(latest["codex"])
    codex["result_sha256"] = "0" * 64
    latest["codex"] = codex
    state.iterations = [latest]
    save_run_state(path, state)
    result_dir = run_artifact_root(store.root, RUN_ID) / LOCAL_FIX_RESULT_DIR
    for item in list(result_dir.glob("*.json")) + list(result_dir.glob("*.commit")):
        item.unlink()
    with pytest.raises(LocalFixAdapterError):
        adapter.execute(
            run_id=RUN_ID,
            effect=effect,
            execution_context=ctx,
            fix_prompt_bytes=FIX_BYTES,
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
        )
    assert store.read_cached_local_fix_result(effect) is None

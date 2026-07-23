"""Phase 16.7 correction round-5 regressions (findings 1-3)."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.helpers import HASH_1, HASH_2, SHA_A

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.paths import set_sensitive_file_mode
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
from ai_dev_loop.pr_review_v2.domain.effects import GeneratePublicationTextEffect
from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import (
    CodexLocalRunnerError,
    FakeCodexProcessRunner,
    PublicationTextRunner,
)
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import (
    CarrierSeed,
    LocalFixAdapter,
    LocalFixAdapterError,
    carrier_run_id,
)
from ai_dev_loop.pr_review_v2.infrastructure.paths import run_artifact_root
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
    LOCAL_FIX_RESULT_DIR,
    ProtectedResultStore,
)
from ai_dev_loop.pr_review_v2_carrier import (
    MAX_STAGED_PATCH_BYTES,
    FilesystemLocalCarrierRuntime,
)
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import RunStatus, save_run_state, sha256_bytes

SESSION = "11111111-1111-1111-1111-111111111111"
RUN_ID = "run-corr5-1"
PLAN_BYTES = b"frozen-plan-for-corr5\n"
PROMPT_BYTES = b"frozen-prompt-for-corr5\n"
PLAN_SHA = sha256_bytes(PLAN_BYTES)
PROMPT_SHA = sha256_bytes(PROMPT_BYTES)
FIX_BYTES = b"fix-prompt-body\n"
PATCH_BYTES = b"diff --git a/x b/x\n+fixed\n"
THREAD_ID = "PRRT_thread_1"


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
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


def _accepted_review_payload() -> dict[str, object]:
    return {
        "has_actionable_findings": False,
        "findings_count": 0,
        "highest_severity": None,
        "cursor_fix_prompt": None,
        "tests_status": "passed",
        "review_markdown": "ok",
        "summary": "no findings",
    }


def _seed_terminal(
    tmp_path: Path,
    *,
    effect_id: str = "eff-term",
    staged_diff_path: str = "git/diffs/01.patch",
    write_patch: bool = True,
    patch_bytes: bytes = PATCH_BYTES,
    patch_sha: str | None = "auto",
    omit_patch_sha: bool = False,
    patch_mode: int = 0o600,
    result_path: str = "codex/reviews/01.json",
    symlink_parent_for_patch: bool = False,
) -> tuple[str, Path, CarrierSeed, FilesystemLocalCarrierRuntime]:
    repo, head = _git_repo(tmp_path)
    runtime = FilesystemLocalCarrierRuntime()
    seed = CarrierSeed(
        v2_run_id=RUN_ID,
        cycle_number=1,
        effect_id=effect_id,
        repository_root=str(repo.resolve()),
        plan_path="plans/x.md",
        prompt_path="prompts/prompt.txt",
        plan_sha256=PLAN_SHA,
        prompt_sha256=PROMPT_SHA,
        plan_bytes=PLAN_BYTES,
        prompt_bytes=PROMPT_BYTES,
        cursor_chat_id="chat-1",
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
    cid = carrier_run_id(RUN_ID, 1, effect_id)
    runtime.ensure_seeded_carrier(carrier_run_id=cid, seed=seed)
    path, state = load_run(cid)

    reviews = path / "codex" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(_accepted_review_payload(), separators=(",", ":")).encode("utf-8")
    result_file = path / result_path
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_bytes(raw)

    if symlink_parent_for_patch:
        outside = tmp_path / "outside-git"
        outside.mkdir()
        real_diffs = outside / "diffs"
        real_diffs.mkdir()
        git_dir = path / "git"
        # Replace git/diffs with a symlink parent escape.
        diffs = git_dir / "diffs"
        if diffs.exists():
            diffs.rmdir() if diffs.is_dir() and not any(diffs.iterdir()) else None
        diffs.symlink_to(real_diffs)

    if (
        write_patch
        and not staged_diff_path.startswith("/")
        and ".." not in Path(staged_diff_path).parts
    ):
        patch_file = path / staged_diff_path
        patch_file.parent.mkdir(parents=True, exist_ok=True)
        if symlink_parent_for_patch:
            # Write through the symlinked parent.
            patch_file.write_bytes(patch_bytes)
        else:
            patch_file.write_bytes(patch_bytes)
            patch_file.chmod(patch_mode)
            if patch_mode == 0o600:
                set_sensitive_file_mode(patch_file)

    git_section: dict[str, object] = {"staged_diff_path": staged_diff_path}
    recorded = sha256_bytes(patch_bytes) if patch_sha == "auto" else patch_sha
    if not omit_patch_sha and recorded is not None:
        git_section["staged_diff_sha256"] = recorded

    iteration: dict[str, object] = {
        "number": 1,
        "kind": "initial_implementation",
        "git": git_section,
        "codex": {
            "events_path": "codex/events/01.jsonl",
            "stderr_path": "codex/events/01.stderr.txt",
            "report_path": "codex/reviews/01.md",
            "metadata_path": "codex/reviews/01.metadata.json",
            "result_path": result_path,
            "result_sha256": sha256_bytes(raw),
            "exit_code": 0,
        },
    }
    state.status = RunStatus.COMPLETED
    state.cursor.chat_id = "chat-1"
    state.iterations = [iteration]
    save_run_state(path, state)
    return cid, path, seed, runtime


def test_terminal_rejects_absolute_and_traversal_patch_paths(tmp_path: Path, isolated_xdg) -> None:
    cid, _path, _seed_obj, runtime = _seed_terminal(
        tmp_path / "abs",
        effect_id="eff-abs",
        staged_diff_path="/tmp/escape.patch",
        write_patch=False,
    )
    with pytest.raises(ValidationError, match="unsafe"):
        runtime.read_terminal_acceptance(cid, expected_session_id=SESSION)

    cid2, _path2, _seed2, runtime2 = _seed_terminal(
        tmp_path / "trav",
        effect_id="eff-trav",
        staged_diff_path="../escape.patch",
        write_patch=False,
    )
    with pytest.raises(ValidationError, match="unsafe"):
        runtime2.read_terminal_acceptance(cid2, expected_session_id=SESSION)


def test_terminal_rejects_symlinked_patch_parent(tmp_path: Path, isolated_xdg) -> None:
    cid, _path, _seed_obj, runtime = _seed_terminal(
        tmp_path,
        staged_diff_path="git/diffs/01.patch",
        symlink_parent_for_patch=True,
    )
    with pytest.raises(ValidationError, match="unsafe"):
        runtime.read_terminal_acceptance(cid, expected_session_id=SESSION)


def test_terminal_rejects_oversized_patch(tmp_path: Path, isolated_xdg) -> None:
    huge = b"x" * (MAX_STAGED_PATCH_BYTES + 1)
    cid, _path, _seed_obj, runtime = _seed_terminal(
        tmp_path, patch_bytes=huge, patch_sha=sha256_bytes(huge)
    )
    with pytest.raises(ValidationError, match="size bound"):
        runtime.read_terminal_acceptance(cid, expected_session_id=SESSION)


def test_terminal_rejects_missing_patch_hash_and_hash_drift(tmp_path: Path, isolated_xdg) -> None:
    cid, _path, _seed_obj, runtime = _seed_terminal(
        tmp_path, effect_id="eff-omit-hash", omit_patch_sha=True
    )
    with pytest.raises(ValidationError, match="missing staged patch hash"):
        runtime.read_terminal_acceptance(cid, expected_session_id=SESSION)

    cid2, path2, _seed2, runtime2 = _seed_terminal(tmp_path / "drift", effect_id="eff-hash-drift")
    _p, state = load_run(cid2)
    latest = dict(state.iterations[0])
    git = dict(latest["git"])
    git["staged_diff_sha256"] = "0" * 64
    latest["git"] = git
    state.iterations = [latest]
    save_run_state(path2, state)
    with pytest.raises(ValidationError, match="staged patch hash mismatch"):
        runtime2.read_terminal_acceptance(cid2, expected_session_id=SESSION)


def test_terminal_rejects_world_readable_patch(tmp_path: Path, isolated_xdg) -> None:
    cid, path, _seed_obj, runtime = _seed_terminal(tmp_path, patch_mode=0o644)
    patch = path / "git" / "diffs" / "01.patch"
    if stat.S_IMODE(patch.stat().st_mode) & 0o077 == 0:
        pytest.skip("filesystem does not preserve group/other mode bits")
    with pytest.raises(ValidationError, match="owner-protected"):
        runtime.read_terminal_acceptance(cid, expected_session_id=SESSION)


def test_snapshot_path_drift_and_traversal_rejected(tmp_path: Path, isolated_xdg) -> None:
    repo, head = _git_repo(tmp_path)
    runtime = FilesystemLocalCarrierRuntime()
    seed = _seed(repo, head)
    cid = carrier_run_id(RUN_ID, 1, "eff-snap")
    runtime.ensure_seeded_carrier(carrier_run_id=cid, seed=seed)
    path, state = load_run(cid)

    state.plan.snapshot_path = "plan/other.md"
    save_run_state(path, state)
    with pytest.raises(ValidationError, match="snapshot_path"):
        runtime.verify_carrier_bindings(carrier_run_id=cid, seed=seed)

    state.plan.snapshot_path = "plan/plan.md"
    state.prompt.snapshot_path = "../escape.txt"
    save_run_state(path, state)
    with pytest.raises(ValidationError, match="snapshot_path|unsafe"):
        runtime.verify_carrier_bindings(carrier_run_id=cid, seed=seed)


def test_same_commit_other_branch_rejected(tmp_path: Path, isolated_xdg) -> None:
    repo, head = _git_repo(tmp_path)
    runtime = FilesystemLocalCarrierRuntime()
    seed = _seed(repo, head)
    cid = carrier_run_id(RUN_ID, 1, "eff-branch")
    runtime.ensure_seeded_carrier(carrier_run_id=cid, seed=seed)
    subprocess.check_call(["git", "checkout", "-b", "other"], cwd=repo, stdout=subprocess.DEVNULL)
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip().lower()
        == head
    )
    with pytest.raises(ValidationError, match="branch"):
        runtime.verify_carrier_bindings(carrier_run_id=cid, seed=seed)


def test_publication_rejects_each_missing_required_field(tmp_path: Path) -> None:
    ctx = _ctx(repo_root=str(tmp_path))
    effect = GeneratePublicationTextEffect(
        effect_id="pr-review:run-corr5-1:cycle:01:generate_publication_text",
        idempotency_key="pr-review:run-corr5-1:cycle:01:generate_publication_text",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_A,
        evidence_ref=ArtifactRef(relative_path="e.json", sha256=HASH_1),
        patch_ref=ArtifactRef(relative_path="p.patch", sha256=HASH_2),
    )
    base = {
        "title": "T",
        "body": "B",
        "commit_subject": "S",
        "commit_body": "C",
    }
    for missing in ("title", "body", "commit_subject", "commit_body"):
        payload = {k: v for k, v in base.items() if k != missing}
        runner = PublicationTextRunner(
            artifact_root=tmp_path / f"arts-{missing}",
            process_runner=FakeCodexProcessRunner(result_payload=payload),
            timeout_seconds=5,
        )
        with pytest.raises(CodexLocalRunnerError, match="schema validation"):
            runner.generate(
                run_id=RUN_ID,
                session_id=SESSION,
                repo_root=str(tmp_path),
                execution_context=ctx,
                evidence_ref=effect.evidence_ref,
                patch_ref=effect.patch_ref,
                effect=effect,
            )


def test_terminal_crash_window_does_not_persist_on_patch_hash_drift(
    tmp_path: Path,
    fake_clis,
    isolated_xdg,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.pr_review_v2.domain.common import LocalFixOutcomeKind, PullRequestBinding
    from ai_dev_loop.pr_review_v2.domain.effects import RunLocalFixEffect

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
        effect_id="pr-review:run-corr5-1:cycle:01:run_local_fix",
        idempotency_key="pr-review:run-corr5-1:cycle:01:run_local_fix",
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
    latest = dict(state.iterations[0])
    git = dict(latest["git"])
    assert isinstance(git.get("staged_diff_sha256"), str)
    git["staged_diff_sha256"] = "0" * 64
    latest["git"] = git
    state.iterations = [latest]
    save_run_state(path, state)
    result_dir = run_artifact_root(store.root, RUN_ID) / LOCAL_FIX_RESULT_DIR
    for item in result_dir.glob("*.json"):
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

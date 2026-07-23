"""Phase 16.7 correction round-6: verified patch evidence on normal acceptance."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.helpers import HASH_1

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
from ai_dev_loop.pr_review_v2.application.write_contracts import DEFAULT_MAX_PATCH_BYTES
from ai_dev_loop.pr_review_v2.domain.common import (
    ArtifactRef,
    PullRequestBinding,
    RepositoryIdentity,
)
from ai_dev_loop.pr_review_v2.domain.effects import RunLocalFixEffect
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import (
    InputArtifactError,
    InputArtifactReader,
)
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import (
    LocalFixAdapter,
    LocalFixAdapterError,
    carrier_run_id,
)
from ai_dev_loop.pr_review_v2.infrastructure.paths import run_artifact_root
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
    LOCAL_FIX_RESULT_DIR,
    ProtectedResultStore,
    ProtectedResultStoreError,
)
from ai_dev_loop.pr_review_v2_carrier import (
    MAX_STAGED_PATCH_BYTES,
    FilesystemLocalCarrierRuntime,
)
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import sha256_bytes

SESSION = "11111111-1111-1111-1111-111111111111"
RUN_ID = "run-corr6-1"
PLAN_BYTES = b"frozen-plan-for-corr6\n"
PROMPT_BYTES = b"frozen-prompt-for-corr6\n"
PLAN_SHA = sha256_bytes(PLAN_BYTES)
PROMPT_SHA = sha256_bytes(PROMPT_BYTES)
FIX_BYTES = b"fix-prompt-body\n"
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


def _ctx(repo_root: str, *, head: str) -> ExecutionContextArtifact:
    return ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from="source_run",
            source_run_id="src-1",
            repository="acme/demo",
            head_branch="main",
            base_branch="main",
            expected_head_sha=head,
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


def test_patch_size_limits_agree_across_surfaces() -> None:
    assert MAX_STAGED_PATCH_BYTES == DEFAULT_MAX_PATCH_BYTES == 8_000_000


def test_persist_and_write_reader_reject_over_limit(tmp_path: Path) -> None:
    store = ProtectedResultStore(tmp_path / "artifacts")
    over = b"x" * (DEFAULT_MAX_PATCH_BYTES + 1)
    with pytest.raises(ProtectedResultStoreError, match="size bound"):
        store.persist_patch_bytes(run_id=RUN_ID, data=over)

    # Exact limit persists; reader accepts exact and rejects +1.
    exact = b"y" * DEFAULT_MAX_PATCH_BYTES
    ref = store.persist_patch_bytes(run_id=RUN_ID, data=exact)
    reader = InputArtifactReader(store.root)
    assert len(reader.read_patch_bytes(run_id=RUN_ID, ref=ref)) == DEFAULT_MAX_PATCH_BYTES

    # Forge an oversized artifact under the run root and prove reader rejects it.
    run_root = run_artifact_root(store.root, RUN_ID)
    forged = run_root / "local" / "patches" / ("f" * 64 + ".patch")
    forged.parent.mkdir(parents=True, exist_ok=True)
    forged.write_bytes(over)
    set_sensitive_file_mode(forged)
    forged_ref = ArtifactRef(
        relative_path=f"local/patches/{forged.name}",
        sha256=sha256_bytes(over),
    )
    with pytest.raises(InputArtifactError, match="maximum size"):
        reader.read_patch_bytes(run_id=RUN_ID, ref=forged_ref)


def test_terminal_and_verified_read_agree_on_size_bound(tmp_path: Path, isolated_xdg) -> None:
    import json

    from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import (
        CarrierSeed,
        carrier_run_id,
    )
    from ai_dev_loop.state import RunStatus, save_run_state

    repo, head = _git_repo(tmp_path)
    runtime = FilesystemLocalCarrierRuntime()
    seed = CarrierSeed(
        v2_run_id=RUN_ID,
        cycle_number=1,
        effect_id="eff-size",
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
    cid = carrier_run_id(RUN_ID, 1, "eff-size")
    runtime.ensure_seeded_carrier(carrier_run_id=cid, seed=seed)
    path, state = load_run(cid)

    exact = b"z" * DEFAULT_MAX_PATCH_BYTES
    over = b"z" * (DEFAULT_MAX_PATCH_BYTES + 1)
    patch_rel = "git/diffs/01.patch"
    (path / "git" / "diffs").mkdir(parents=True, exist_ok=True)
    patch_file = path / patch_rel
    patch_file.write_bytes(exact)
    set_sensitive_file_mode(patch_file)

    review = {
        "has_actionable_findings": False,
        "findings_count": 0,
        "highest_severity": None,
        "cursor_fix_prompt": None,
        "tests_status": "passed",
        "review_markdown": "ok",
        "summary": "ok",
    }
    raw = json.dumps(review, separators=(",", ":")).encode("utf-8")
    result_rel = "codex/reviews/01.json"
    (path / "codex" / "reviews").mkdir(parents=True, exist_ok=True)
    (path / result_rel).write_bytes(raw)

    state.status = RunStatus.COMPLETED
    state.cursor.chat_id = "chat-1"
    state.iterations = [
        {
            "number": 1,
            "kind": "initial_implementation",
            "git": {
                "staged_diff_path": patch_rel,
                "staged_diff_sha256": sha256_bytes(exact),
            },
            "codex": {
                "result_path": result_rel,
                "result_sha256": sha256_bytes(raw),
                "exit_code": 0,
            },
        }
    ]
    save_run_state(path, state)

    accepted = runtime.read_terminal_acceptance(cid, expected_session_id=SESSION)
    assert accepted is not None
    assert len(accepted.patch_bytes) == DEFAULT_MAX_PATCH_BYTES
    verified = runtime.read_verified_staged_patch_bytes(cid, patch_rel)
    assert verified == exact

    patch_file.write_bytes(over)
    set_sensitive_file_mode(patch_file)
    # Keep recorded hash for exact so oversize fails on size, not hash.
    with pytest.raises(ValidationError, match="size bound"):
        runtime.read_verified_staged_patch_bytes(cid, patch_rel)
    with pytest.raises(ValidationError, match="size bound"):
        runtime.read_terminal_acceptance(cid, expected_session_id=SESSION)


def test_mutated_patch_after_finalization_cannot_accept(
    tmp_path: Path,
    fake_clis,
    isolated_xdg,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutate carrier patch after accepted finalization; normal mapping must fail closed."""

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)

    class MutatingRuntime(FilesystemLocalCarrierRuntime):
        def read_verified_staged_patch_bytes(
            self, carrier_run_id: str, relative_path: str
        ) -> bytes:
            path, _state = load_run(carrier_run_id)
            patch_path = path / relative_path
            if patch_path.is_file():
                patch_path.write_bytes(b"diff --git a/x b/x\n+TAMPERED\n")
                set_sensitive_file_mode(patch_path)
            return super().read_verified_staged_patch_bytes(carrier_run_id, relative_path)

    repo, head = _git_repo(tmp_path)
    store = ProtectedResultStore(tmp_path / "artifacts")
    ctx = _ctx(str(repo.resolve()), head=head)
    ctx_ref = store.persist_execution_context(run_id=RUN_ID, artifact=ctx)
    store.persist_source_plan_bytes(run_id=RUN_ID, data=PLAN_BYTES)
    store.persist_source_prompt_bytes(run_id=RUN_ID, data=PROMPT_BYTES)
    fix_ref = store.persist_fix_prompt(run_id=RUN_ID, text=FIX_BYTES.decode())
    effect = RunLocalFixEffect(
        effect_id="pr-review:run-corr6-1:cycle:01:run_local_fix",
        idempotency_key="pr-review:run-corr6-1:cycle:01:run_local_fix",
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
    adapter = LocalFixAdapter(runtime=MutatingRuntime(), store=store)
    with pytest.raises(LocalFixAdapterError, match="staged patch verification"):
        adapter.execute(
            run_id=RUN_ID,
            effect=effect,
            execution_context=ctx,
            fix_prompt_bytes=FIX_BYTES,
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
        )
    assert store.read_cached_local_fix_result(effect) is None
    result_dir = run_artifact_root(store.root, RUN_ID) / LOCAL_FIX_RESULT_DIR
    assert not list(result_dir.glob("*.json")) if result_dir.exists() else True
    # Carrier finalizer did run and recorded a hash before mapping failed.
    cid = carrier_run_id(RUN_ID, 1, effect.effect_id)
    _path, state = load_run(cid)
    assert state.iterations
    git = state.iterations[0].get("git")
    assert isinstance(git, dict)
    assert isinstance(git.get("staged_diff_sha256"), str)

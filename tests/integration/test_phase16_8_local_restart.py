"""Phase 16.8 local carrier restart through the real subordinate boundary."""

from __future__ import annotations

import os
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.integration.phase16_8_checkpoint_helpers import (
    install_blocking_agent,
    install_blocking_codex,
    wait_for_path,
)
from tests.integration.phase16_8_helpers import SESSION

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
from ai_dev_loop.pr_review_v2.domain.common import PullRequestBinding, RepositoryIdentity
from ai_dev_loop.pr_review_v2.domain.effects import RunLocalFixEffect
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import (
    LocalFixAdapter,
    carrier_run_id,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
    LOCAL_FIX_RESULT_DIR,
    ProtectedResultStore,
)
from ai_dev_loop.pr_review_v2_carrier import FilesystemLocalCarrierRuntime
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import RunStatus, sha256_bytes

RUN_ID = "run-p168-local"
PLAN_BYTES = b"frozen-plan\n"
PROMPT_BYTES = b"frozen-prompt\n"
THREAD_ID = "PRRT_thread_1"
T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _native_tmpdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setenv("TMP", "/tmp")
    monkeypatch.setenv("TEMP", "/tmp")


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root / "cache"))
    return root


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


def _ctx(*, repo_root: str, head: str) -> ExecutionContextArtifact:
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
            plan_sha256=sha256_bytes(PLAN_BYTES),
            prompt_path="prompts/prompt.txt",
            prompt_sha256=sha256_bytes(PROMPT_BYTES),
        ),
        repository_root=repo_root,
    )


def test_carrier_run_id_is_deterministic_per_effect() -> None:
    effect_id = "pr-review:run-p168-local:cycle:01:run_local_fix"
    first = carrier_run_id(RUN_ID, 1, effect_id)
    second = carrier_run_id(RUN_ID, 1, effect_id)
    assert first == second
    assert first.startswith("prv2c-")


def test_terminal_carrier_replay_reconstructs_without_agent_call(
    tmp_path: Path,
    fake_clis,
    isolated_xdg,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    repo, head = _git_repo(tmp_path)
    store = ProtectedResultStore(tmp_path / "artifacts")
    ctx = _ctx(repo_root=str(repo.resolve()), head=head)
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
    fix_prompt = b"Codex adjudicated fix\n"
    fix_ref = store.persist_fix_prompt(run_id=RUN_ID, text=fix_prompt.decode())
    effect = RunLocalFixEffect(
        effect_id="pr-review:run-p168-local:cycle:01:run_local_fix",
        idempotency_key="pr-review:run-p168-local:cycle:01:run_local_fix",
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
        fix_prompt_bytes=fix_prompt,
        plan_bytes=PLAN_BYTES,
        prompt_bytes=PROMPT_BYTES,
    )
    assert first.outcome.value == "accepted"
    carrier_id = carrier_run_id(RUN_ID, 1, effect.effect_id)
    _path, state = load_run(carrier_id)
    assert state.status in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_RESIDUAL_RISK}
    assert state.codex.session_id == SESSION

    from ai_dev_loop.pr_review_v2.infrastructure.paths import run_artifact_root

    result_dir = run_artifact_root(store.root, RUN_ID) / LOCAL_FIX_RESULT_DIR
    for path in list(result_dir.glob("*.json")) + list(result_dir.glob("*.commit")):
        path.unlink()
    assert store.read_cached_local_fix_result(effect) is None

    calls = {"n": 0}

    def _boom(request):  # noqa: ANN001
        calls["n"] += 1
        raise AssertionError("agent boundary must not rerun for terminal replay")

    monkeypatch.setattr(
        "ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter.run_local_review_fix",
        _boom,
    )
    second = adapter.execute(
        run_id=RUN_ID,
        effect=effect,
        execution_context=ctx,
        fix_prompt_bytes=fix_prompt,
        plan_bytes=PLAN_BYTES,
        prompt_bytes=PROMPT_BYTES,
    )
    assert calls["n"] == 0
    assert second.outcome.value == "accepted"
    cached = store.read_cached_local_fix_result(effect)
    assert cached is not None
    assert cached.cursor_chat_id == state.cursor.chat_id


def test_blocking_fake_agent_and_codex_use_readiness_ipc(tmp_path: Path) -> None:
    from tests.integration.phase16_8_checkpoint_helpers import (
        install_blocking_agent,
        wait_for_path,
    )

    ready_agent = tmp_path / "agent.ready"
    ready_codex = tmp_path / "codex.ready"
    proceed = tmp_path / "proceed"
    bin_dir = tmp_path / "bin"
    install_blocking_agent(bin_dir, ready_path=ready_agent, proceed_path=proceed)
    install_blocking_codex(bin_dir, ready_path=ready_codex, proceed_path=proceed)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}

    def _run_agent() -> None:
        subprocess.run(
            [str(bin_dir / "agent"), "-p", "prompt"],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )

    thread = threading.Thread(target=_run_agent)
    thread.start()
    wait_for_path(ready_agent, timeout_seconds=5.0)
    proceed.write_text("go\n", encoding="utf-8")
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    subprocess.run(
        [str(bin_dir / "codex"), "exec", "resume", "session", "-"],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    assert ready_codex.is_file()


def _local_fix_bundle(
    tmp_path: Path,
    *,
    repo: Path,
    head: str,
    fake_clis: dict[str, Path],
) -> tuple[
    ProtectedResultStore, ExecutionContextArtifact, RunLocalFixEffect, bytes, LocalFixAdapter
]:
    store = ProtectedResultStore(tmp_path / "artifacts")
    ctx = _ctx(repo_root=str(repo.resolve()), head=head)
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
    fix_prompt = b"Codex adjudicated fix\n"
    fix_ref = store.persist_fix_prompt(run_id=RUN_ID, text=fix_prompt.decode())
    effect = RunLocalFixEffect(
        effect_id="pr-review:run-p168-local:cycle:01:run_local_fix",
        idempotency_key="pr-review:run-p168-local:cycle:01:run_local_fix",
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
    del fake_clis
    return store, ctx, effect, fix_prompt, adapter


def test_carrier_seed_checkpoint_preserves_identity_via_blocking_ipc(
    tmp_path: Path,
    fake_clis,
    isolated_xdg,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After carrier seed, blocking agent IPC at mid-cursor preserves carrier/chat/session."""

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    repo, head = _git_repo(tmp_path)
    _store, ctx, effect, fix_prompt, adapter = _local_fix_bundle(
        tmp_path, repo=repo, head=head, fake_clis=fake_clis
    )
    carrier_id = carrier_run_id(RUN_ID, 1, effect.effect_id)
    ready_agent = tmp_path / "agent.ready"
    proceed = tmp_path / "proceed"
    bin_dir = tmp_path / "blocking-bin"
    install_blocking_agent(bin_dir, ready_path=ready_agent, proceed_path=proceed)
    monkeypatch.setenv("PATH", f"{bin_dir}:{fake_clis['bin_dir']}:{os.environ.get('PATH', '')}")

    result_holder: dict[str, object] = {}

    def _run() -> None:
        result_holder["outcome"] = adapter.execute(
            run_id=RUN_ID,
            effect=effect,
            execution_context=ctx,
            fix_prompt_bytes=fix_prompt,
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
        )

    thread = threading.Thread(target=_run)
    thread.start()
    wait_for_path(ready_agent, timeout_seconds=5.0)
    _path, seeded_state = load_run(carrier_id)
    assert seeded_state.codex.session_id == SESSION
    seeded_chat = seeded_state.cursor.chat_id
    assert seeded_chat
    proceed.write_text("go\n", encoding="utf-8")
    thread.join(timeout=30.0)
    assert not thread.is_alive()
    outcome = result_holder.get("outcome")
    assert outcome is not None
    assert outcome.outcome.value == "accepted"  # type: ignore[union-attr]
    _path2, terminal_state = load_run(carrier_id)
    assert _path == _path2
    assert terminal_state.codex.session_id == SESSION
    assert terminal_state.cursor.chat_id == seeded_chat
    assert carrier_run_id(RUN_ID, 1, effect.effect_id) == carrier_id


@pytest.mark.parametrize(
    "checkpoint",
    __import__(
        "tests.integration.phase16_8_checkpoint_helpers", fromlist=["CARRIER_CHECKPOINTS"]
    ).CARRIER_CHECKPOINTS,
)
def test_carrier_checkpoint_crash_reopen_preserves_identity(
    tmp_path: Path,
    fake_clis,
    isolated_xdg,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
) -> None:
    """Kill/reopen at each carrier boundary; one carrier, exact chat/session, monotonic iterations."""

    from tests.integration.phase16_8_carrier_helpers import run_carrier_until_checkpoint_then_resume

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    repo, head = _git_repo(tmp_path)
    store, ctx, effect, fix_prompt, adapter = _local_fix_bundle(
        tmp_path, repo=repo, head=head, fake_clis=fake_clis
    )
    carrier_id = carrier_run_id(RUN_ID, 1, effect.effect_id)
    ready = tmp_path / f"ready-{checkpoint}.txt"
    proceed = tmp_path / f"proceed-{checkpoint}.txt"
    if proceed.is_file():
        proceed.unlink()
    from tests.integration.phase16_8_carrier_helpers import blocking_cli_path

    blocking_cli_path(
        tmp_path,
        fake_clis,
        monkeypatch,
        checkpoint=checkpoint,  # type: ignore[arg-type]
        ready_path=ready,
        proceed_path=proceed,
    )
    app_before = (repo / "app.py").read_text(encoding="utf-8")
    outcome = run_carrier_until_checkpoint_then_resume(
        checkpoint=checkpoint,  # type: ignore[arg-type]
        adapter=adapter,
        run_id=RUN_ID,
        effect=effect,
        execution_context=ctx,
        fix_prompt=fix_prompt,
        carrier_id=carrier_id,
        ready_path=ready,
        proceed_path=proceed,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        fake_clis=fake_clis,
    )
    assert outcome.outcome.value == "accepted"
    assert carrier_run_id(RUN_ID, 1, effect.effect_id) == carrier_id
    _path, terminal = load_run(carrier_id)
    assert terminal.codex.session_id == SESSION
    assert terminal.cursor.chat_id
    assert (repo / "app.py").read_text(encoding="utf-8") == app_before


def test_v2_result_before_claim_complete_reopen(tmp_path: Path) -> None:
    from tests.integration.phase16_4_matrix_helpers import claim_next
    from tests.integration.phase16_8_checkpoint_helpers import reopen_engine
    from tests.integration.phase16_8_local_engine_helpers import (
        build_prepared_local_engine,
        make_executor,
        publication_payload,
    )
    from tests.unit.pr_review_v2.durable_helpers import FakeClock

    from ai_dev_loop.pr_review_v2.application.contracts import (
        EffectCompletionRequest,
        EventDisposition,
    )
    from ai_dev_loop.pr_review_v2.domain.events import EffectSucceeded
    from ai_dev_loop.pr_review_v2.infrastructure.codex_local_runners import FakeCodexProcessRunner

    clock = FakeClock()
    engine_root = tmp_path / "engine-v2"
    engine_root.mkdir()
    repo = engine_root / "repo"
    repo.mkdir()
    db_path = engine_root / "engine.sqlite3"
    engine, run_id, store, ctx_ref = build_prepared_local_engine(
        engine_root, clock, repo_root=str(repo.resolve())
    )
    lease, claim = claim_next(engine, run_id)
    runner = FakeCodexProcessRunner(result_payload=publication_payload())
    executor = make_executor(store, ctx_ref=ctx_ref, process_runner=runner, run_id=run_id)
    event = executor.execute(claim.effect, claim.completion_token, now=clock.now())
    assert isinstance(event, EffectSucceeded)
    engine2 = reopen_engine(db_path, clock, prefix="p168-v2-result-reopen")
    replay = FakeCodexProcessRunner(result_payload={"title": "must-not-run"})
    executor2 = make_executor(store, ctx_ref=ctx_ref, process_runner=replay, run_id=run_id)
    cached = executor2.execute(claim.effect, claim.completion_token, now=clock.now())
    assert isinstance(cached, EffectSucceeded)
    assert replay.last_argv is None
    receipt = engine2.complete_claim(
        EffectCompletionRequest(
            submission_id="v2-result-after-reopen",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id=lease.owner_id,
            lease_generation=lease.generation,
            event=event,
        )
    )
    assert receipt.disposition is EventDisposition.ACCEPTED

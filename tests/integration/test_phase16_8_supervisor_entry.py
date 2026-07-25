"""Phase 16.8 supervisor worker entry point and launcher recovery."""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.integration.stateful_fake_gh import StatefulFakeGhController
from tests.unit.pr_review_v2.durable_helpers import FakeClock

from ai_dev_loop.pr_review_v2.application.control import ControlPlaneService
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
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
from ai_dev_loop.pr_review_v2.application.preparation import PreparationService, SourceRunSnapshot
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    default_engine_db_path,
    pr_review_v2_state_dir,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SequenceIdFactory
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore
from ai_dev_loop.pr_review_v2.workers.supervisor import (
    PrReviewV2Supervisor,
    SupervisorLauncherMetadata,
    SupervisorLauncherStore,
)

PLAN_BYTES = b"frozen-plan-for-supervisor\n"
PROMPT_BYTES = b"frozen-prompt-for-supervisor\n"
PLAN_SHA = hashlib.sha256(PLAN_BYTES).hexdigest()
PROMPT_SHA = hashlib.sha256(PROMPT_BYTES).hexdigest()
T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
SESSION = "11111111-1111-1111-1111-111111111111"
SHA_A = "a" * 40


@pytest.fixture(autouse=True)
def _native_tmpdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setenv("TMP", "/tmp")
    monkeypatch.setenv("TEMP", "/tmp")


@pytest.fixture
def pr_review_v2_env(tmp_path: Path, fake_clis, monkeypatch: pytest.MonkeyPatch):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg / "cache"))
    gh = StatefulFakeGhController(tmp_path / "gh-state.json")
    gh_path = gh.install(tmp_path / "gh-bin")
    repo = tmp_path / "repo"
    repo.mkdir()
    env_path = f"{fake_clis['bin_dir']}:{gh_path.parent}:{os.environ.get('PATH', '')}"
    monkeypatch.setenv("PATH", env_path)
    clock = FakeClock(T0)
    db_path = default_engine_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = PrReviewEngine(
        SqlitePrReviewStore(db_path),
        clock=clock,
        ids=SequenceIdFactory(prefix="sup-entry"),
    )
    artifact_root = pr_review_v2_state_dir() / "artifacts"
    arts = ProtectedResultStore(artifact_root)
    ctx = ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from="source_run",
            source_run_id="src-sup",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
        ),
        cursor=ExecutionContextCursor(
            chat_id="chat-sup",
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
        ),
        repository_root=str(repo.resolve()),
    )
    prep = PreparationService(engine, arts, clock=clock)
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-sup",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"diff --git a/x b/x\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=ctx,
        )
    )
    launchers = SupervisorLauncherStore(artifact_root)
    control = ControlPlaneService(
        engine,
        artifact_store=arts,
        launcher_store=launchers,
        spawner=lambda _rid: "spawned",
    )
    return {
        "engine": engine,
        "arts": arts,
        "launchers": launchers,
        "control": control,
        "created": created,
        "repo": repo,
    }


def test_supervisor_worker_entry_point_runs_bounded_steps(
    pr_review_v2_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = pr_review_v2_env["created"]
    launchers = pr_review_v2_env["launchers"]
    control = pr_review_v2_env["control"]
    token = "token-sup-entry"
    control.start(created.run_id)
    launchers.write(
        SupervisorLauncherMetadata(
            schema_version=1,
            run_id=created.run_id,
            token=token,
            pid=os.getpid(),
            pgid=os.getpgid(0),
            process_start_time=str(os.getpid()),
            executable=str(Path(sys.executable).resolve()),
            created_at=T0.isoformat(),
        )
    )

    real_supervisor = PrReviewV2Supervisor

    def _bounded_supervisor(*args, **kwargs):
        kwargs.setdefault("max_steps", 1)
        kwargs.setdefault("idle_poll_seconds", 0.01)
        return real_supervisor(*args, **kwargs)

    monkeypatch.setattr(
        "ai_dev_loop.pr_review_v2_supervisor_worker.PrReviewV2Supervisor",
        _bounded_supervisor,
    )
    from ai_dev_loop.pr_review_v2_supervisor_worker import main

    code = main([created.run_id, token])
    assert code == 0
    assert launchers.read(created.run_id) is None


def test_supervisor_worker_rejects_missing_launcher_metadata(
    pr_review_v2_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = pr_review_v2_env["created"]
    from ai_dev_loop.pr_review_v2_supervisor_worker import main

    code = main([created.run_id, "token-missing"])
    assert code == 1

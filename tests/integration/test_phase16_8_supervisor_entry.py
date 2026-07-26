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


def _worker_ctx(
    *,
    repo_root: str,
    lease_ttl_seconds: int,
    heartbeat_interval_seconds: int,
    prepared_from: str = "existing_pr",
    source_run_id: str | None = None,
) -> ExecutionContextArtifact:
    return ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from=prepared_from,
            source_run_id=source_run_id,
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
                lease_ttl_seconds=lease_ttl_seconds,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                idle_poll_seconds=1,
            ),
        ),
        plan_prompt=ExecutionContextPlanPrompt(
            plan_path="plans/x.md",
            plan_sha256=PLAN_SHA,
            prompt_path="prompts/prompt.txt",
            prompt_sha256=PROMPT_SHA,
        ),
        repository_root=repo_root,
    )


@pytest.mark.parametrize(
    ("lease_ttl_seconds", "heartbeat_interval_seconds"),
    [
        (120, 30),
        (90, 20),
    ],
)
def test_assemble_supervisor_runtime_uses_frozen_lease_ttl(
    tmp_path: Path,
    fake_clis,
    monkeypatch: pytest.MonkeyPatch,
    lease_ttl_seconds: int,
    heartbeat_interval_seconds: int,
) -> None:
    """Gate B regression: operational engine must use frozen TTL, not default 30."""

    from datetime import timedelta

    from ai_dev_loop.pr_review_v2.application.preparation import ExistingPrSnapshot
    from ai_dev_loop.pr_review_v2.domain import PullRequestBinding, RepositoryIdentity
    from ai_dev_loop.pr_review_v2.runtime_factory import assemble_supervisor_runtime

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg / "cache"))
    gh = StatefulFakeGhController(tmp_path / "gh-state.json")
    gh_path = gh.install(tmp_path / "gh-bin")
    monkeypatch.setenv(
        "PATH",
        f"{fake_clis['bin_dir']}:{gh_path.parent}:{os.environ.get('PATH', '')}",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    clock = FakeClock(T0)
    db_path = default_engine_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = PrReviewEngine(
        SqlitePrReviewStore(db_path),
        clock=clock,
        ids=SequenceIdFactory(prefix="ttl-wire"),
        lease_ttl=timedelta(seconds=lease_ttl_seconds),
    )
    arts = ProtectedResultStore(pr_review_v2_state_dir() / "artifacts")
    binding = PullRequestBinding(
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        pr_number=4,
        head_branch="feature",
        base_branch="main",
        head_sha=SHA_A,
    )
    created = PreparationService(engine, arts, clock=clock).prepare_existing_pr(
        ExistingPrSnapshot(
            binding=binding,
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=_worker_ctx(
                repo_root=str(repo.resolve()),
                lease_ttl_seconds=lease_ttl_seconds,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
            ),
        )
    )
    runtime = assemble_supervisor_runtime(created.run_id)
    assert runtime.engine.lease_ttl == timedelta(seconds=lease_ttl_seconds)
    assert runtime.engine.lease_ttl != timedelta(seconds=30) or lease_ttl_seconds == 30
    assert runtime.execution_context.pr_review_v2.worker.lease_ttl_seconds == lease_ttl_seconds
    # Construction must succeed for the frozen heartbeat/lease pair (no claim required).
    assert runtime.worker is not None


def test_effect_worker_rejects_mismatched_heartbeat_before_lease_or_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid heartbeat/engine TTL must fail before any durable mutation."""

    from datetime import timedelta

    from ai_dev_loop.pr_review_v2.application.contracts import EventSubmission
    from ai_dev_loop.pr_review_v2.domain.events import StartRequested
    from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock(T0)
    db_path = tmp_path / "mismatch.sqlite3"
    engine = PrReviewEngine(
        SqlitePrReviewStore(db_path),
        clock=clock,
        ids=SequenceIdFactory(prefix="hb-mismatch"),
        lease_ttl=timedelta(seconds=30),
    )
    arts = ProtectedResultStore(tmp_path / "artifacts")
    repo = tmp_path / "repo"
    repo.mkdir()
    created = PreparationService(engine, arts, clock=clock).create_from_source(
        SourceRunSnapshot(
            source_run_id="src-hb-mismatch",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"diff --git a/x b/x\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=_worker_ctx(
                repo_root=str(repo.resolve()),
                lease_ttl_seconds=30,
                heartbeat_interval_seconds=10,
                prepared_from="source_run",
                source_run_id="src-hb-mismatch",
            ),
        )
    )
    engine.apply_event(
        EventSubmission(
            submission_id="start-hb",
            run_id=created.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    with engine.store.begin_read() as conn:
        before_leases = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM pr_review_worker_leases WHERE run_id=?",
                (created.run_id,),
            ).fetchone()["n"]
        )
        before_claims = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM pr_review_effects WHERE run_id=? AND status='claimed'",
                (created.run_id,),
            ).fetchone()["n"]
        )
        before_events = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM pr_review_events WHERE run_id=?",
                (created.run_id,),
            ).fetchone()["n"]
        )

    class _NeverExecutor:
        calls = 0

        def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.calls += 1
            raise AssertionError("executor must not run for mismatched heartbeat")

    executor = _NeverExecutor()
    with pytest.raises(ValueError, match="strictly less than lease_ttl"):
        EffectWorker(
            engine,
            executor,  # type: ignore[arg-type]
            owner_id="bad-owner",
            heartbeat_interval=timedelta(seconds=30),
        )
    assert executor.calls == 0
    with engine.store.begin_read() as conn:
        assert (
            int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM pr_review_worker_leases WHERE run_id=?",
                    (created.run_id,),
                ).fetchone()["n"]
            )
            == before_leases
        )
        assert (
            int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM pr_review_effects WHERE run_id=? AND status='claimed'",
                    (created.run_id,),
                ).fetchone()["n"]
            )
            == before_claims
        )
        assert (
            int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM pr_review_events WHERE run_id=?",
                    (created.run_id,),
                ).fetchone()["n"]
            )
            == before_events
        )
    status = engine.get_status(created.run_id)
    assert status.effect_status is not None
    assert status.effect_status.value == "pending"


def _gate_b_prepare_claimed_trigger(
    *,
    tmp_path: Path,
    fake_clis,
    monkeypatch: pytest.MonkeyPatch,
    lease_ttl_seconds: int = 120,
    heartbeat_interval_seconds: int = 30,
) -> dict:
    """Create ExistingPr run, start it, claim request_bot_review, then 'die'."""

    from datetime import timedelta

    from ai_dev_loop.pr_review_v2.application.contracts import EventDisposition, EventSubmission
    from ai_dev_loop.pr_review_v2.application.preparation import ExistingPrSnapshot
    from ai_dev_loop.pr_review_v2.domain import PullRequestBinding, RepositoryIdentity
    from ai_dev_loop.pr_review_v2.domain.events import StartRequested

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg / "cache"))
    gh = StatefulFakeGhController(tmp_path / "gh-state.json")
    gh_path = gh.install(tmp_path / "gh-bin")
    monkeypatch.setenv(
        "PATH",
        f"{fake_clis['bin_dir']}:{gh_path.parent}:{os.environ.get('PATH', '')}",
    )
    monkeypatch.setenv("STATEFUL_GH_STATE", str(tmp_path / "gh-state.json"))
    repo = tmp_path / "repo"
    repo.mkdir()
    clock = FakeClock(T0)
    db_path = default_engine_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = PrReviewEngine(
        SqlitePrReviewStore(db_path),
        clock=clock,
        ids=SequenceIdFactory(prefix="gate-b"),
        lease_ttl=timedelta(seconds=lease_ttl_seconds),
    )
    arts = ProtectedResultStore(pr_review_v2_state_dir() / "artifacts")
    binding = PullRequestBinding(
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        pr_number=4,
        head_branch="feature",
        base_branch="main",
        head_sha=SHA_A,
    )
    created = PreparationService(engine, arts, clock=clock).prepare_existing_pr(
        ExistingPrSnapshot(
            binding=binding,
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=_worker_ctx(
                repo_root=str(repo.resolve()),
                lease_ttl_seconds=lease_ttl_seconds,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
            ),
        )
    )
    # Production reconcile validates PR identity via fake gh fixture.
    gh.state.fixture = {
        "owner": "acme",
        "name": "demo",
        "pr_number": 4,
        "head_sha": SHA_A,
        "head_branch": "feature",
        "base_branch": "main",
    }
    gh.state.issue_comments = []
    gh.state.save(gh.state_path)
    receipt = engine.apply_event(
        EventSubmission(
            submission_id=f"start:{created.run_id}:1",
            run_id=created.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    assert receipt.disposition is EventDisposition.ACCEPTED
    lease = engine.acquire_lease(created.run_id, "dying-supervisor")
    claim = engine.claim_next_effect(created.run_id, "dying-supervisor", lease.generation).claim
    assert claim is not None
    assert claim.effect.kind == "request_bot_review"
    assert claim.classification == "mutating"
    # Supervisor dies before any external write: leave claim open.
    mutations_before = dict(gh.reload().mutation_counts)
    assert mutations_before.get("create_issue_comment", 0) == 0
    return {
        "run_id": created.run_id,
        "db_path": db_path,
        "clock": clock,
        "gh": gh,
        "claim": claim,
        "lease_generation": lease.generation,
        "effect_id": claim.effect.effect_id,
        "dispatch_id": claim.dispatch_id,
        "marker": claim.effect.marker,
        "lease_ttl_seconds": lease_ttl_seconds,
    }


def test_gate_b_crash_after_request_bot_review_claim_reconciles_one_trigger(
    tmp_path: Path,
    fake_clis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact Gate B window: claimed, no write, expire, reopen, one trigger."""

    from datetime import UTC, datetime, timedelta

    from ai_dev_loop.pr_review_v2.application.control import ControlPlaneService
    from ai_dev_loop.pr_review_v2.infrastructure.runtime import SystemClock
    from ai_dev_loop.pr_review_v2.runtime_factory import assemble_supervisor_runtime
    from ai_dev_loop.pr_review_v2.workers.supervisor import SupervisorLauncherStore

    harness = _gate_b_prepare_claimed_trigger(
        tmp_path=tmp_path, fake_clis=fake_clis, monkeypatch=monkeypatch
    )
    run_id = harness["run_id"]
    claim = harness["claim"]
    original_effect_id = harness["effect_id"]
    original_dispatch = harness["dispatch_id"]
    original_lease_gen = harness["lease_generation"]
    gh = harness["gh"]

    with SqlitePrReviewStore(harness["db_path"]).begin_read() as conn:
        start_count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM pr_review_events "
                "WHERE run_id=? AND event_kind='start_requested'",
                (run_id,),
            ).fetchone()["n"]
        )
    assert start_count == 1

    # Process/runtime close: reopen via corrected production assembly.
    fake_clock = FakeClock(datetime.now(tz=UTC))
    monkeypatch.setattr(SystemClock, "now", lambda self: fake_clock.now())
    runtime = assemble_supervisor_runtime(run_id)
    assert runtime.engine.lease_ttl == timedelta(seconds=120)

    # Expired mutating claim must enter reconciliation first (no blind replay).
    step1 = runtime.worker.run_once(run_id)
    assert step1.claimed is True
    assert step1.effect_kind == "reconcile_write"
    status_after_reconcile = runtime.engine.get_status(run_id)
    assert status_after_reconcile.state_kind == "waiting_retry"
    assert status_after_reconcile.lease_generation > original_lease_gen
    assert gh.reload().mutation_counts.get("create_issue_comment", 0) == 0

    with runtime.engine.store.begin_read() as conn:
        orig_row = conn.execute(
            "SELECT status, effect_id FROM pr_review_effects WHERE run_id=? AND dispatch_id=?",
            (run_id, original_dispatch),
        ).fetchone()
    assert orig_row is not None
    assert orig_row["effect_id"] == original_effect_id
    assert orig_row["status"] in {"uncertain", "blocked", "succeeded"}

    # Become eligible and emit exactly one authorized trigger.
    eligible = status_after_reconcile.next_eligible_at
    assert eligible is not None
    fake_clock.set(eligible)
    runtime.engine.fire_due_timers_for_run(run_id)
    step2 = runtime.worker.run_once(run_id)
    assert step2.claimed is True
    assert step2.effect_kind == "request_bot_review"
    assert step2.completed is True
    assert gh.reload().mutation_counts.get("create_issue_comment", 0) == 1

    # Another tick must not duplicate the trigger or StartRequested.
    step3 = runtime.worker.run_once(run_id)
    assert gh.reload().mutation_counts.get("create_issue_comment", 0) == 1
    with runtime.engine.store.begin_read() as conn:
        starts = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM pr_review_events "
                "WHERE run_id=? AND event_kind='start_requested'",
                (run_id,),
            ).fetchone()["n"]
        )
        effect_row = conn.execute(
            "SELECT effect_id, idempotency_key FROM pr_review_effects "
            "WHERE run_id=? AND effect_kind='request_bot_review' "
            "ORDER BY dispatch_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    assert starts == 1
    assert effect_row is not None
    assert effect_row["effect_id"] == original_effect_id

    launchers = SupervisorLauncherStore(runtime.artifact_root)
    control = ControlPlaneService(
        runtime.engine,
        artifact_store=ProtectedResultStore(runtime.artifact_root),
        launcher_store=launchers,
        spawner=lambda _rid: "spawned",
    )
    status = control.status(run_id)
    assert status.run_id == run_id
    # Resume repair must not reapply StartRequested.
    resumed = control.resume(run_id)
    assert resumed.transition_applied is False
    assert resumed.supervisor_action in {"spawned", "repaired"}
    with runtime.engine.store.begin_read() as conn:
        assert (
            int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM pr_review_events "
                    "WHERE run_id=? AND event_kind='start_requested'",
                    (run_id,),
                ).fetchone()["n"]
            )
            == 1
        )
    assert gh.reload().mutation_counts.get("create_issue_comment", 0) == 1
    del claim, step3


@pytest.mark.parametrize(
    "reconcile_outcome",
    ["APPLIED", "UNRESOLVED"],
)
def test_gate_b_reconcile_applied_and_unresolved_at_production_boundary(
    tmp_path: Path,
    fake_clis,
    monkeypatch: pytest.MonkeyPatch,
    reconcile_outcome: str,
) -> None:
    """Complementary Gate B boundary: APPLIED no duplicate; UNRESOLVED fail-closed."""

    from datetime import UTC, datetime, timedelta

    from ai_dev_loop.pr_review_v2.application.write_contracts import html_comment_marker
    from ai_dev_loop.pr_review_v2.domain.common import PauseReasonKind
    from ai_dev_loop.pr_review_v2.infrastructure.runtime import SystemClock
    from ai_dev_loop.pr_review_v2.runtime_factory import assemble_supervisor_runtime

    harness = _gate_b_prepare_claimed_trigger(
        tmp_path=tmp_path, fake_clis=fake_clis, monkeypatch=monkeypatch
    )
    run_id = harness["run_id"]
    marker = harness["marker"]
    gh = harness["gh"]
    needle = html_comment_marker(marker)

    if reconcile_outcome == "APPLIED":
        state = gh.reload()
        state.issue_comments.append(
            {
                "id": "IC_applied_gate_b",
                "databaseId": 9001,
                "body": f"@codex review\n{needle}",
                "createdAt": "2026-07-21T12:01:00Z",
                "author": {"login": "orchestrator"},
            }
        )
        state.save(gh.state_path)
    else:
        state = gh.reload()
        state.issue_comments.extend(
            [
                {
                    "id": "IC_dup_a",
                    "databaseId": 9002,
                    "body": needle,
                    "createdAt": "2026-07-21T12:00:00Z",
                    "author": {"login": "a"},
                },
                {
                    "id": "IC_dup_b",
                    "databaseId": 9003,
                    "body": needle,
                    "createdAt": "2026-07-21T12:01:00Z",
                    "author": {"login": "b"},
                },
            ]
        )
        state.save(gh.state_path)

    fake_clock = FakeClock(datetime.now(tz=UTC))
    monkeypatch.setattr(SystemClock, "now", lambda self: fake_clock.now())
    runtime = assemble_supervisor_runtime(run_id)
    assert runtime.engine.lease_ttl == timedelta(seconds=120)
    step = runtime.worker.run_once(run_id)
    assert step.claimed is True
    assert step.effect_kind == "reconcile_write"
    status = runtime.engine.get_status(run_id)
    mutations = gh.reload().mutation_counts.get("create_issue_comment", 0)
    assert mutations == 0
    if reconcile_outcome == "APPLIED":
        assert status.state_kind == "waiting_for_bot"
        assert status.active_effect_kind == "observe_bot_review"
        # No duplicate trigger write after APPLIED proof.
        runtime.worker.run_once(run_id)
        assert gh.reload().mutation_counts.get("create_issue_comment", 0) == 0
    else:
        assert status.state_kind == "paused"
        with runtime.engine.store.begin_read() as conn:
            snap, _, _ = runtime.engine.store.load_validated_snapshot(conn, run_id)
        assert snap.reason is PauseReasonKind.AMBIGUOUS_WRITE_UNRESOLVED
        runtime.worker.run_once(run_id)
        assert gh.reload().mutation_counts.get("create_issue_comment", 0) == 0
        assert runtime.engine.get_status(run_id).state_kind == "paused"

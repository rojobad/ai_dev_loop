"""Phase 27.7 cross-feature recovery acceptance through public CLI callers."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

import ai_dev_loop.cli as cli
from ai_dev_loop.cli import app
from ai_dev_loop.process import StreamingProcessResult
from ai_dev_loop.scheduler import codex_implementer_attempt_runner
from ai_dev_loop.scheduler.application.codex_capacity_probe import (
    CodexCapacityObservation,
    CodexCapacityStatus,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import FakeAgentProcessBackend
from ai_dev_loop.scheduler.application.git_admission import BoundedGitAdmissionPort
from ai_dev_loop.scheduler.application.implementer_failure_recovery import (
    load_implementer_recovery_record,
)
from ai_dev_loop.scheduler.application.implementer_timeout_retry import (
    ImplementerRetryService,
)
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.sequence import (
    AbortedSequenceState,
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    load_sequence_run_lineage,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
IMPLEMENTER_ID = "019def00-0000-0000-0000-0000000007aa"
REVIEWER_ONE_ID = "019def00-0000-0000-0000-0000000007b1"
REVIEWER_TWO_ID = "019def00-0000-0000-0000-0000000007b2"
FIX_PROMPT = "Preserve this exact correction prompt.\r\nUnicode: 雪λ.\n"
EXPECTED_RECOVERY_BYTES = b"1:timeout\n2:quota\n3:failure\n4:success\n5:success\n"


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "gpt-5.6-sol",
                        "supported_reasoning_efforts": ["high", "xhigh"],
                    },
                    {"id": "gpt-6.1-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    root = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": root / "engine.sqlite3", "artifact_root": root / "artifacts"}


@dataclass
class _ScriptedImplementer:
    """Deterministic provider transport shared by restarted scheduler backends."""

    modes: list[str]
    calls: int = 0
    argv: list[list[str]] = field(default_factory=list)
    prompts: list[bytes] = field(default_factory=list)


class _ProductionAdapterBackend(FakeAgentProcessBackend):
    """Run the production implementer adapter with deterministic process outcomes."""

    def __init__(self, script: _ScriptedImplementer) -> None:
        super().__init__()
        self.script = script

    def _execute_cursor_attempt(self, request):  # type: ignore[no-untyped-def]
        if "ai_dev_loop.scheduler.codex_implementer_attempt_runner" not in request.agent_argv:
            return super()._execute_cursor_attempt(request)

        def fake_process(argv: list[str], **kwargs: object) -> StreamingProcessResult:
            index = self.script.calls
            self.script.calls += 1
            mode = self.script.modes[min(index, len(self.script.modes) - 1)]
            prompt_bytes = str(kwargs["stdin_text"]).encode("utf-8")
            self.script.argv.append(list(argv))
            self.script.prompts.append(prompt_bytes)

            stdout_path = kwargs["stdout_path"]
            stderr_path = kwargs["stderr_path"]
            workspace = Path(str(kwargs["cwd"]))
            assert isinstance(stdout_path, Path)
            assert isinstance(stderr_path, Path)
            events: list[dict[str, object]] = []
            if "resume" not in argv:
                events.append({"type": "thread.started", "thread_id": IMPLEMENTER_ID})

            target = workspace / "phase27-7-recovery.txt"
            with target.open("a", encoding="utf-8") as handle:
                handle.write(f"{index + 1}:{mode}\n")

            if mode == "timeout":
                returncode, timed_out, stderr = -15, True, "controlled timeout"
            elif mode == "quota":
                events.extend(
                    [
                        {"type": "error", "message": "usage_limit_exceeded"},
                        {
                            "type": "turn.failed",
                            "error": {"message": "usage_limit_exceeded"},
                        },
                    ]
                )
                returncode, timed_out, stderr = 2, False, "controlled quota wait"
            elif mode == "failure":
                returncode, timed_out, stderr = 2, False, "controlled generic failure"
            else:
                events.append(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "implemented"},
                    }
                )
                returncode, timed_out, stderr = 0, False, ""

            stdout_path.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
            stderr_path.write_text(stderr, encoding="utf-8")
            return StreamingProcessResult(
                args=argv,
                returncode=returncode,
                stdout="",
                stderr=stderr,
                timed_out=timed_out,
                elapsed_seconds=30.0,
            )

        with patch(
            "ai_dev_loop.scheduler.codex_implementer_attempt_runner.run_process_streaming",
            fake_process,
        ):
            return codex_implementer_attempt_runner.main(request.agent_argv[3:])


class _StaticProbe:
    def __init__(self, status: CodexCapacityStatus) -> None:
        self.status = status

    def probe(self, codex_command: str) -> CodexCapacityObservation:
        assert codex_command
        return CodexCapacityObservation(status=self.status)


def _tick(
    paths: dict[str, Path],
    script: _ScriptedImplementer,
    *,
    owner: str,
    capacity: CodexCapacityStatus = CodexCapacityStatus.EXHAUSTED,
) -> TickService:
    return TickService(
        SqliteSchedulerStore(paths["db_path"]),
        ProtectedArtifactStore(paths["artifact_root"]),
        BoundedGitAdmissionPort(),
        now_factory=lambda: NOW,
        tick_owner_factory=lambda: owner,
        attempt_backend=_ProductionAdapterBackend(script),
        codex_capacity_probe=_StaticProbe(capacity),
    )


def _run_state(paths: dict[str, Path], run_id: str):  # type: ignore[no-untyped-def]
    store = SqliteSchedulerStore.open_readonly(paths["db_path"])
    with store.begin_read() as conn:
        return store.load_validated_snapshot(conn, run_id)[0]


def _sequence_state(paths: dict[str, Path], sequence_id: str):  # type: ignore[no-untyped-def]
    store = SqliteSchedulerStore.open_readonly(paths["db_path"])
    with store.begin_read() as conn:
        return store.load_validated_sequence_state(conn, sequence_id)


def _reservation(paths: dict[str, Path], run_id: str):  # type: ignore[no-untyped-def]
    store = SqliteSchedulerStore.open_readonly(paths["db_path"])
    with store.begin_read() as conn:
        return store.get_reservation_for_run(conn, run_id)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _git_bytes(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=repo)


def _invoke(runner: CliRunner, args: list[str], *, input_text: str | None = None) -> dict:
    result = runner.invoke(app, args, input=input_text)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _drive_run(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tick: TickService,
    paths: dict[str, Path],
    run_id: str,
    predicate,
    *,
    limit: int = 100,
):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cli, "run_scheduler_tick", tick.run_once)
    for _ in range(limit):
        _invoke(runner, ["scheduler", "tick", "--output", "json"])
        state = _run_state(paths, run_id)
        if predicate(state):
            return state
    raise AssertionError(f"run {run_id} did not reach expected checkpoint")


def _patch_retry_cli(
    monkeypatch: pytest.MonkeyPatch,
    paths: dict[str, Path],
) -> None:
    import ai_dev_loop.scheduler.application.implementer_timeout_retry as retry_module

    def retry(run_id: str, force: bool = False):  # type: ignore[no-untyped-def]
        return ImplementerRetryService(
            SqliteSchedulerStore(paths["db_path"]),
            ProtectedArtifactStore(paths["artifact_root"]),
            now_factory=lambda: NOW,
        ).retry(run_id, force=force)

    monkeypatch.setattr(retry_module, "scheduler_implementer_retry", retry)


def _submit_standalone(runner: CliRunner, repo: Path, *, prompt: str) -> str:
    response = _invoke(
        runner,
        [
            "scheduler",
            "submit",
            "--short-name",
            "Recovery acceptance",
            "--repo-path",
            str(repo),
            "--plan-path",
            "docs/plans/sample-plan.md",
            "--prompt-source-path",
            "docs/plans/prompt_sample-plan.txt",
            "--implementer-provider",
            "codex",
            "--implementer-model",
            "gpt-5.6-sol",
            "--implementer-reasoning-effort",
            "xhigh",
            "--implementer-timeout-minutes",
            "19",
            "--codex-review-model",
            "gpt-6.1-sol",
            "--codex-review-reasoning-effort",
            "high",
            "--max-review-iterations",
            "3",
            "--output",
            "json",
        ],
        input_text=prompt,
    )
    run_id = str(response["run_id"])
    _invoke(runner, ["scheduler", "start", run_id, "--output", "json"])
    return run_id


def _prepare_sequence(runner: CliRunner, repo: Path) -> tuple[str, str]:
    manifest = repo.parent / "phase27-7-sequence.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "name": "phase27-7-recovery-acceptance",
                "short_name": "Recovery acceptance",
                "phases": [
                    {
                        "name": "codex-recovery",
                        "short_name": "Codex recovery",
                        "plan_path": "docs/plans/sample-plan.md",
                        "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                        "commit_message": "accepted recovery checkpoint",
                        "workflow": {"max_review_iterations": 3},
                        "implementer": {
                            "provider": "codex",
                            "model": "gpt-5.6-sol",
                            "reasoning_effort": "xhigh",
                            "timeout_minutes": 19,
                        },
                        "codex": {
                            "review_model": "gpt-6.1-sol",
                            "review_reasoning_effort": "high",
                        },
                    },
                    {
                        "name": "cursor-final",
                        "short_name": "Codex final",
                        "plan_path": "docs/plans/sample-plan.md",
                        "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                        "implementer": {
                            "provider": "codex",
                            "model": "gpt-5.6-sol",
                            "reasoning_effort": "xhigh",
                            "timeout_minutes": 19,
                        },
                        "codex": {
                            "review_model": "gpt-6.1-sol",
                            "review_reasoning_effort": "high",
                        },
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    prepared = _invoke(
        runner,
        [
            "scheduler",
            "sequence",
            "prepare",
            "--repo-path",
            str(repo),
            "--manifest",
            str(manifest),
            "--output",
            "json",
        ],
    )
    sequence_id = str(prepared["sequence_id"])
    started = _invoke(
        runner,
        ["scheduler", "sequence", "start", sequence_id, "--output", "json"],
    )
    return sequence_id, str(started["run_id"])


def _drive_recovery_chain_to_blocked(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    paths: dict[str, Path],
    script: _ScriptedImplementer,
    run_id: str,
):  # type: ignore[no-untyped-def]
    first_tick = _tick(paths, script, owner="phase27-7-before-restart")
    timed_out = _drive_run(
        runner,
        monkeypatch,
        first_tick,
        paths,
        run_id,
        lambda state: (
            getattr(state, "codex", None) is not None
            and state.codex.implementer_timeout_continuation is not None
        ),
    )
    timeout = timed_out.codex.implementer_timeout_continuation
    assert timeout.session_id == IMPLEMENTER_ID
    assert timeout.automatic_retries == 1
    assert timeout.wait_until == "2026-10-15T12:30:00.000000Z"
    assert _reservation(paths, run_id) is not None

    _patch_retry_cli(monkeypatch, paths)
    receipt = _invoke(
        runner,
        ["scheduler", "implementer-retry", run_id, "--output", "json"],
    )
    assert receipt["run_id"] == run_id
    assert receipt["provider"] == "codex"
    assert receipt["changed"] is True
    assert receipt["automatic_retries"] == 0

    restarted = _tick(paths, script, owner="phase27-7-capacity-restart")
    waiting = _drive_run(
        runner,
        monkeypatch,
        restarted,
        paths,
        run_id,
        lambda state: state.kind == "waiting_codex_capacity",
    )
    continuation = waiting.codex.capacity_continuation
    assert continuation is not None
    assert continuation.role == "implementer" and continuation.turn == "initial"
    assert continuation.session_id == IMPLEMENTER_ID
    assert _reservation(paths, run_id) is not None

    calls_at_wait = script.calls
    _drive_run(
        runner,
        monkeypatch,
        restarted,
        paths,
        run_id,
        lambda state: state.kind == "waiting_codex_capacity",
        limit=1,
    )
    assert script.calls == calls_at_wait

    available_after_restart = _tick(
        paths,
        script,
        owner="phase27-7-capacity-available",
        capacity=CodexCapacityStatus.AVAILABLE,
    )
    blocked = _drive_run(
        runner,
        monkeypatch,
        available_after_restart,
        paths,
        run_id,
        lambda state: state.kind == "blocked",
    )
    assert blocked.block_reason_kind == "codex_implementer_failure"
    return blocked


def test_standalone_timeout_capacity_force_correction_acceptance(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    prompt = (git_repo / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    script = _ScriptedImplementer(["timeout", "quota", "failure", "success", "success"])
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_ONE_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_FIX_PROMPT", FIX_PROMPT)
    run_id = _submit_standalone(runner, git_repo, prompt=prompt)

    blocked = _drive_recovery_chain_to_blocked(runner, monkeypatch, scheduler_paths, script, run_id)
    immutable_source = blocked.model_dump_json()
    _patch_retry_cli(monkeypatch, scheduler_paths)
    forced = _invoke(
        runner,
        ["scheduler", "implementer-retry", run_id, "--force", "--output", "json"],
    )
    successor = str(forced["run_id"])
    assert forced == {
        "run_id": successor,
        "provider": "codex",
        "changed": True,
        "available_at": None,
        "automatic_retries": 0,
    }
    replay = _invoke(
        runner,
        ["scheduler", "implementer-retry", run_id, "--force", "--output", "json"],
    )
    assert replay["run_id"] == successor and replay["changed"] is False
    assert _run_state(scheduler_paths, run_id).model_dump_json() == immutable_source
    assert _reservation(scheduler_paths, run_id) is None
    assert _reservation(scheduler_paths, successor) is not None

    after_publication_restart = _tick(
        scheduler_paths,
        script,
        owner="phase27-7-successor-restart",
        capacity=CodexCapacityStatus.AVAILABLE,
    )
    completed = _drive_run(
        runner,
        monkeypatch,
        after_publication_restart,
        scheduler_paths,
        successor,
        lambda state: state.kind == "completed",
    )

    assert _run_state(scheduler_paths, run_id).model_dump_json() == immutable_source
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    assert completed.codex.reviewer_session_id == REVIEWER_ONE_ID
    assert completed.codex.reviews_completed == 2
    assert completed.context.workflow.max_review_iterations == 3
    assert completed.context.implementer.model == "gpt-5.6-sol"
    assert completed.context.implementer.reasoning_effort == "xhigh"
    assert completed.context.implementer.timeout_minutes == 19
    assert completed.context.codex.review_model == "gpt-6.1-sol"
    assert completed.context.codex.review_reasoning_effort == "high"
    assert _reservation(scheduler_paths, successor) is None
    assert script.calls == 5
    assert _git_bytes(git_repo, "show", ":phase27-7-recovery.txt") == EXPECTED_RECOVERY_BYTES

    original = prompt.encode("utf-8")
    assert script.prompts[:4] == [original, original, original, original]
    assert script.prompts[4].endswith(FIX_PROMPT.encode("utf-8"))
    assert all("--last" not in argv for argv in script.argv)
    assert "resume" not in script.argv[0]
    assert all("resume" in argv and IMPLEMENTER_ID in argv for argv in script.argv[1:])
    assert all(
        "gpt-5.6-sol" in argv and 'model_reasoning_effort="xhigh"' in argv for argv in script.argv
    )

    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        record = load_implementer_recovery_record(store, artifacts, conn, successor)
        relation_count = conn.execute(
            "SELECT COUNT(*) FROM scheduler_codex_implementer_recoveries WHERE source_run_id=?",
            (run_id,),
        ).fetchone()[0]
    assert record is not None
    assert record.implementer.session_id == IMPLEMENTER_ID
    assert record.exact_prompt.sha256 == hashlib.sha256(original).hexdigest()
    assert record.timeout_automatic_retries_consumed == 0
    assert relation_count == 1
    fix_path = artifacts.run_root(successor) / "prompts/fixes/01.txt"
    assert fix_path.read_bytes() == FIX_PROMPT.encode("utf-8")
    assert _git(git_repo, "diff", "--cached", "--name-only")
    assert not _git(git_repo, "diff", "--name-only")
    assert "--last" not in fake_clis["codex_log"].read_text(encoding="utf-8")


def test_codex_only_v3_sequence_recovery_keeps_checkpoint_and_frozen_authority(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    script = _ScriptedImplementer(["timeout", "quota", "failure", "success", "success"])
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_ONE_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_FIX_PROMPT", FIX_PROMPT)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    base = _git(git_repo, "rev-parse", "HEAD")
    sequence_id, source = _prepare_sequence(runner, git_repo)

    blocked = _drive_recovery_chain_to_blocked(runner, monkeypatch, scheduler_paths, script, source)
    immutable_source = blocked.model_dump_json()
    blocked_sequence = _sequence_state(scheduler_paths, sequence_id)
    assert blocked_sequence.current_ordinal == 1
    assert blocked_sequence.current_run_id == source

    _patch_retry_cli(monkeypatch, scheduler_paths)
    forced = _invoke(
        runner,
        ["scheduler", "implementer-retry", source, "--force", "--output", "json"],
    )
    successor = str(forced["run_id"])
    replay = _invoke(
        runner,
        ["scheduler", "implementer-retry", source, "--force", "--output", "json"],
    )
    assert replay["run_id"] == successor and replay["changed"] is False
    adopted = _sequence_state(scheduler_paths, sequence_id)
    assert isinstance(adopted, ActiveSequenceState)
    assert adopted.current_ordinal == 1 and adopted.current_run_id == successor
    assert _run_state(scheduler_paths, source).model_dump_json() == immutable_source

    restarted = _tick(
        scheduler_paths,
        script,
        owner="phase27-7-sequence-successor-restart",
        capacity=CodexCapacityStatus.AVAILABLE,
    )
    monkeypatch.setattr(cli, "run_scheduler_tick", restarted.run_once)
    for _ in range(180):
        sequence = _sequence_state(scheduler_paths, sequence_id)
        if isinstance(sequence, AwaitingFinalizationSequenceState):
            break
        if sequence.current_ordinal == 2:
            monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_TWO_ID)
        _invoke(runner, ["scheduler", "tick", "--output", "json"])
    else:
        raise AssertionError("Codex-only sequence did not reach awaiting_finalization")

    assert isinstance(sequence, AwaitingFinalizationSequenceState)
    assert len(sequence.materialized_entries) == 2
    assert _git(git_repo, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert _git(git_repo, "log", "-1", "--pretty=%s") == "accepted recovery checkpoint"
    assert _git_bytes(git_repo, "show", "HEAD:phase27-7-recovery.txt") == EXPECTED_RECOVERY_BYTES
    assert _git_bytes(git_repo, "show", ":phase27-7-recovery.txt") == (
        EXPECTED_RECOVERY_BYTES + b"6:success\n"
    )
    assert _git(git_repo, "diff", "--cached", "--name-only")
    assert not _git(git_repo, "diff", "--name-only")

    recovered = _run_state(scheduler_paths, successor)
    final = _run_state(scheduler_paths, sequence.final_run_id)
    assert recovered.kind == "completed" and recovered.codex.reviews_completed == 2
    assert recovered.codex.implementer_session_id == IMPLEMENTER_ID
    assert recovered.codex.reviewer_session_id == REVIEWER_ONE_ID
    assert final.kind == "completed"
    assert final.context.implementer.provider == "codex"
    assert final.codex.implementer_session_id == IMPLEMENTER_ID
    assert final.codex.reviewer_session_id == REVIEWER_TWO_ID
    assert final.codex.reviewer_session_id != recovered.codex.reviewer_session_id
    assert script.calls == 6

    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        lineage = load_sequence_run_lineage(conn, sequence_id, definition=sequence.definition)
        active_reservations = conn.execute(
            "SELECT COUNT(*) FROM scheduler_repository_reservations WHERE status='active'"
        ).fetchone()[0]
    assert [item.attempt_kind for item in lineage.phase_executions[0].attempts] == [
        "planned_run",
        "implementer_retry",
    ]
    assert [item.run_id for item in lineage.phase_executions[0].attempts] == [source, successor]
    assert active_reservations == 0
    assert script.prompts[0] == script.prompts[1] == script.prompts[2] == script.prompts[3]
    assert script.prompts[4].endswith(FIX_PROMPT.encode("utf-8"))


def test_sequence_abort_during_restarted_capacity_wait_preserves_partial_work(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    script = _ScriptedImplementer(["timeout", "quota", "success"])
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_ONE_ID)
    base = _git(git_repo, "rev-parse", "HEAD")
    sequence_id, run_id = _prepare_sequence(runner, git_repo)

    first_tick = _tick(scheduler_paths, script, owner="phase27-7-abort-timeout")
    _drive_run(
        runner,
        monkeypatch,
        first_tick,
        scheduler_paths,
        run_id,
        lambda state: (
            getattr(state, "codex", None) is not None
            and state.codex.implementer_timeout_continuation is not None
        ),
    )
    _patch_retry_cli(monkeypatch, scheduler_paths)
    _invoke(runner, ["scheduler", "implementer-retry", run_id, "--output", "json"])
    restarted = _tick(scheduler_paths, script, owner="phase27-7-abort-capacity-restart")
    waiting = _drive_run(
        runner,
        monkeypatch,
        restarted,
        scheduler_paths,
        run_id,
        lambda state: state.kind == "waiting_codex_capacity",
    )
    assert waiting.codex.capacity_continuation.session_id == IMPLEMENTER_ID
    assert _reservation(scheduler_paths, run_id) is not None
    partial_before = (git_repo / "phase27-7-recovery.txt").read_bytes()
    calls_before_abort = script.calls

    import ai_dev_loop.scheduler.application.sequence_abort as abort_module

    production_abort = abort_module.scheduler_sequence_abort
    monkeypatch.setattr(
        abort_module,
        "scheduler_sequence_abort",
        lambda requested_sequence_id: production_abort(
            requested_sequence_id,
            db_path=scheduler_paths["db_path"],
            artifact_root=scheduler_paths["artifact_root"],
            backend=_ProductionAdapterBackend(script),
        ),
    )
    receipt = _invoke(
        runner,
        ["scheduler", "sequence", "abort", sequence_id, "--output", "json"],
    )
    assert receipt["sequence_id"] == sequence_id

    after_abort_restart = _tick(
        scheduler_paths,
        script,
        owner="phase27-7-after-abort-restart",
        capacity=CodexCapacityStatus.AVAILABLE,
    )
    monkeypatch.setattr(cli, "run_scheduler_tick", after_abort_restart.run_once)
    for _ in range(8):
        _invoke(runner, ["scheduler", "tick", "--output", "json"])

    sequence = _sequence_state(scheduler_paths, sequence_id)
    assert isinstance(sequence, AbortedSequenceState)
    assert len(sequence.materialized_entries) == 1
    assert _run_state(scheduler_paths, run_id).kind == "aborted"
    assert _reservation(scheduler_paths, run_id) is None
    assert script.calls == calls_before_abort
    assert (git_repo / "phase27-7-recovery.txt").read_bytes() == partial_before
    assert _git(git_repo, "rev-parse", "HEAD") == base
    assert _git(git_repo, "rev-list", "--count", f"{base}..HEAD") == "0"
    assert (
        SequenceStatusService(SqliteSchedulerStore(scheduler_paths["db_path"]))
        .get_status(sequence_id)
        .state_kind
        == "aborted"
    )

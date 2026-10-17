"""Phase 27.3 production sequence smoke and frozen input contracts (fake CLIs)."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
import yaml
from pydantic import ValidationError as ModelValidationError
from referencing import Registry, Resource

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.paths import DIR_MODE, SENSITIVE_FILE_MODE, schema_path
from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.git_admission import BoundedGitAdmissionPort
from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService
from ai_dev_loop.scheduler.application.sequence_materializer import frozen_entry_hash
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    prepare_sequence,
)
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.sequence import SequenceManifest
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.6-sol", "supported_reasoning_efforts": ["high", "xhigh"]},
                    {"id": "gpt-6.1-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        )
    )
    root = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": root / "engine.sqlite3", "artifact_root": root / "artifacts"}


def _tick(repo, paths, *, backend=None):
    return TickService(
        SqliteSchedulerStore(paths["db_path"]),
        ProtectedArtifactStore(paths["artifact_root"]),
        BoundedGitAdmissionPort(),
        now_factory=lambda: datetime(2026, 10, 14, 20, 0, tzinfo=UTC),
        attempt_backend=backend or FakeAgentProcessBackend(),
    )


def manifest_payload(providers: tuple[str, ...], *, version: int = 3) -> dict:
    phases = []
    for ordinal, provider in enumerate(providers, 1):
        phase = {
            "name": f"phase-{ordinal}",
            "plan_path": "docs/plans/sample-plan.md",
            "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
            "codex": {"review_model": "gpt-6.1-sol", "review_reasoning_effort": "high"},
        }
        if version == 3:
            phase["short_name"] = f"Phase {ordinal}"
        if ordinal < len(providers):
            phase["commit_message"] = f"accepted checkpoint {ordinal}"
        if provider == "codex":
            phase["implementer"] = {
                "provider": "codex",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "xhigh",
                "timeout_minutes": 17 + ordinal,
            }
        phases.append(phase)
    payload = {"schema_version": version, "name": "dual-provider-smoke", "phases": phases}
    if version == 3:
        payload["short_name"] = "Dual provider smoke"
    return payload


def prepare(
    repo: Path,
    paths: dict[str, Path],
    providers: tuple[str, ...],
    *,
    version: int = 3,
    payload: dict | None = None,
):
    manifest = repo.parent / "sequence.yaml"
    manifest.write_text(
        yaml.safe_dump(payload or manifest_payload(providers, version=version), sort_keys=False)
    )
    return prepare_sequence(SequencePrepareOptions(repo_path=repo, manifest_path=manifest, **paths))


def sequence_state(paths: dict[str, Path], sequence_id: str):
    store = SqliteSchedulerStore.open_readonly(paths["db_path"])
    with store.begin_read() as conn:
        return store.load_validated_sequence_state(conn, sequence_id)


def leaf_state(paths: dict[str, Path], run_id: str):
    store = SqliteSchedulerStore.open_readonly(paths["db_path"])
    with store.begin_read() as conn:
        return store.load_validated_snapshot(conn, run_id)[0]


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def drive(tick, paths, sequence_id, monkeypatch, *, target="awaiting_finalization", limit=220):
    for _ in range(limit):
        seq = sequence_state(paths, sequence_id)
        if target == "awaiting_finalization" and hasattr(seq, "finalized_at"):
            return seq
        ordinal = seq.current_ordinal
        monkeypatch.setenv(
            "FAKE_CODEX_IMPLEMENTER_SESSION_ID", f"019def00-0000-0000-0000-{ordinal:012x}"
        )
        monkeypatch.setenv(
            "FAKE_CODEX_REVIEWER_SESSION_ID", f"019def00-0000-0000-0001-{ordinal:012x}"
        )
        monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", f"codex-phase-{ordinal}.txt")
        tick.run_once()
        leaf = leaf_state(paths, seq.current_run_id)
        if leaf.kind == target:
            return leaf
        if leaf.kind == "blocked":
            raise AssertionError(f"blocked {leaf.block_reason_kind}: {leaf.block_reason_summary}")
    raise AssertionError(f"sequence did not reach {target}")


@pytest.mark.parametrize("providers", [("cursor", "codex", "cursor"), ("codex", "cursor", "codex")])
def test_mixed_three_phase_smoke_freezes_distinct_sessions_and_two_checkpoints(
    git_repo,
    scheduler_paths,
    fake_clis,
    monkeypatch,
    providers,
):
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    base = git(git_repo, "rev-parse", "HEAD")
    status_before = git(git_repo, "status", "--porcelain")
    result = prepare(git_repo, scheduler_paths, providers)
    assert git(git_repo, "status", "--porcelain") == status_before
    assert not fake_clis["codex_log"].exists()
    seq = sequence_state(scheduler_paths, result.sequence_id)
    assert seq.definition.schema_version == 2
    assert [e.implementer.provider for e in seq.definition.entries] == list(providers)
    start_sequence(result.sequence_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    final = drive(tick, scheduler_paths, result.sequence_id, monkeypatch)
    assert git(git_repo, "rev-list", "--count", f"{base}..HEAD") == "2"
    assert git(git_repo, "diff", "--cached", "--name-only")
    assert not git(git_repo, "diff", "--name-only")
    assert len(final.materialized_entries) == 3
    sessions = set()
    for materialized, frozen in zip(
        final.materialized_entries, final.definition.entries, strict=True
    ):
        leaf = leaf_state(scheduler_paths, materialized.run_id)
        assert leaf.kind == "completed"
        assert leaf.context.schema_version == 6
        assert leaf.context.sequence.entry_hash == frozen_entry_hash(frozen)
        assert leaf.context.implementer == frozen.implementer
        assert leaf.context.codex.review_model == "gpt-6.1-sol"
        assert leaf.context.codex.review_reasoning_effort == "high"
        sessions.add(leaf.codex.reviewer_session_id)
        if frozen.implementer.provider == "codex":
            assert leaf.context.implementer.model == "gpt-5.6-sol"
            assert leaf.context.implementer.reasoning_effort == "xhigh"
            assert leaf.context.implementer.timeout_minutes == 17 + frozen.ordinal
            assert leaf.codex.implementer_session_id != leaf.codex.reviewer_session_id
            sessions.add(leaf.codex.implementer_session_id)
    assert len(sessions) == 3 + providers.count("codex")
    with tick.store.begin_read() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_repository_reservations WHERE status = 'active'"
            ).fetchone()[0]
            == 0
        )
    log = fake_clis["codex_log"].read_text()
    assert "gpt-5.6-sol" in log and "xhigh" in log and "gpt-6.1-sol" in log
    assert "--last" not in log


@pytest.mark.parametrize("turn", ["initial", "correction"])
@pytest.mark.parametrize("providers", [("codex", "cursor"), ("cursor", "codex", "cursor")])
def test_capacity_wait_preserves_leaf_and_resumes_after_restart(
    git_repo,
    scheduler_paths,
    fake_clis,
    monkeypatch,
    tmp_path,
    turn,
    providers,
):
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_SEQUENCE",
        "usage_limit,success" if turn == "initial" else "success,usage_limit,success",
    )
    ordinal = providers.index("codex") + 1
    reviews = "no_findings" if turn == "initial" else "findings,no_findings"
    if ordinal == 2:
        reviews = "no_findings," + reviews
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", reviews)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    correction = "Fix exact bytes.\r\nUnicode: 雪λ.\n\r\n"
    monkeypatch.setenv("FAKE_CODEX_FIX_PROMPT", correction)
    witnesses = tmp_path / "witnesses"
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_STDIN_WITNESS_DIR", str(witnesses))
    result = prepare(git_repo, scheduler_paths, providers)
    start_sequence(result.sequence_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    head = git(git_repo, "rev-parse", "HEAD")
    waiting = drive(
        tick, scheduler_paths, result.sequence_id, monkeypatch, target="waiting_codex_capacity"
    )
    seq = sequence_state(scheduler_paths, result.sequence_id)
    continuation = waiting.codex.capacity_continuation
    root = ProtectedArtifactStore(scheduler_paths["artifact_root"]).run_root(waiting.run_id)
    prompt_bytes = (root / continuation.prompt_artifact_path).read_bytes()
    assert continuation.role == "implementer" and continuation.turn == turn
    assert waiting.codex.reviews_completed == (0 if turn == "initial" else 1)
    if turn == "correction":
        assert (root / "prompts/fixes/01.txt").read_bytes() == correction.encode()
        assert prompt_bytes.endswith(correction.encode())
    assert seq.current_ordinal == ordinal and len(seq.materialized_entries) == ordinal
    assert git(git_repo, "rev-list", "--count", f"{head}..HEAD") == str(ordinal - 1)
    wait_head = git(git_repo, "rev-parse", "HEAD")
    assert (git_repo / f"codex-phase-{ordinal}.txt").is_file()
    with tick.store.begin_read() as conn:
        assert tick.store.get_reservation_for_run(conn, waiting.run_id) is not None
    # Restart reads durable state, never performs clean-baseline admission again.
    backend = FakeAgentProcessBackend()
    restarted = _tick(git_repo, scheduler_paths, backend=backend)
    before_attempts = len(backend.launch_calls)
    for capacity in ("exhausted", "unavailable"):
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", capacity)
        restarted.run_once()
        assert leaf_state(scheduler_paths, waiting.run_id).kind == "waiting_codex_capacity"
        assert len(backend.launch_calls) == before_attempts
        assert git(git_repo, "rev-parse", "HEAD") == wait_head
    status = SequenceStatusService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    ).get_status(result.sequence_id)
    assert status.current_implementer_provider == "codex"
    assert status.codex_pending_role == "implementer" and status.codex_pending_turn == turn
    assert "scheduler tick" in status.safe_next_action.command
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    final = drive(restarted, scheduler_paths, result.sequence_id, monkeypatch)
    completed = leaf_state(scheduler_paths, waiting.run_id)
    assert completed.codex.implementer_session_id == continuation.session_id
    assert completed.codex.reviews_completed == (1 if turn == "initial" else 2)
    recorded = sorted(witnesses.glob("*.bin"))
    assert recorded[-2].read_bytes() == prompt_bytes == recorded[-1].read_bytes()
    assert len(final.materialized_entries) == len(providers)
    assert git(git_repo, "rev-list", "--count", f"{head}..HEAD") == str(len(providers) - 1)


def test_sequence_abort_during_capacity_wait_keeps_partial_work_and_no_successor(
    git_repo, scheduler_paths, monkeypatch
):
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    result = prepare(git_repo, scheduler_paths, ("codex", "cursor"))
    start_sequence(result.sequence_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    waiting = drive(
        tick, scheduler_paths, result.sequence_id, monkeypatch, target="waiting_codex_capacity"
    )
    head = git(git_repo, "rev-parse", "HEAD")
    SequenceAbortService(tick.store).abort_sequence(result.sequence_id)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    backend = FakeAgentProcessBackend()
    restarted = _tick(git_repo, scheduler_paths, backend=backend)
    for _ in range(5):
        restarted.run_once()
    seq = sequence_state(scheduler_paths, result.sequence_id)
    assert seq.aborted_at and len(seq.materialized_entries) == 1
    assert leaf_state(scheduler_paths, waiting.run_id).kind == "aborted"
    assert (git_repo / "codex-phase-1.txt").is_file()
    assert git(git_repo, "rev-parse", "HEAD") == head
    assert not backend.launch_calls


def validate_schema(payload, name):
    schemas = [json.loads(p.read_text()) for p in schema_path(name).parent.glob("*.json")]
    registry = Registry().with_resources(
        (s["$id"], Resource.from_contents(s)) for s in schemas if "$id" in s
    )
    jsonschema.Draft202012Validator(
        json.loads(schema_path(name).read_text()), registry=registry
    ).validate(payload)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {"provider": None},
        {"provider": "unknown"},
        {"provider": "codex", "model": ""},
        {"provider": "codex", "reasoning_effort": "invalid"},
        {"provider": "codex", "timeout_minutes": True},
        {"provider": "codex", "timeout_minutes": 0},
        {"provider": "cursor", "model": "gpt-5.6-sol"},
    ],
)
def test_manifest_rejects_null_invalid_and_conflicting_fields(bad):
    payload = manifest_payload(("codex", "cursor"), version=2)
    payload["phases"][0]["implementer"] = bad
    with pytest.raises(ModelValidationError):
        SequenceManifest.model_validate(payload)
    with pytest.raises(jsonschema.ValidationError):
        validate_schema(payload, "scheduler-sequence-manifest-v2.json")


def test_prepare_frozen_inputs_idempotence_config_precedence_and_drift(
    git_repo, scheduler_paths, monkeypatch
):
    config_path = git_repo / "phase-config.yaml"
    (git_repo / ".git/info/exclude").write_text("phase-config.yaml\n")
    config = yaml.safe_load((git_repo / "ai_dev_loop.yaml").read_text())
    config["implementer"] = {
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "timeout_minutes": 41,
    }
    config_path.write_text(yaml.safe_dump(config))
    payload = manifest_payload(("codex", "cursor"))
    payload["phases"][0]["implementer"] = {"provider": "codex", "reasoning_effort": "xhigh"}
    manifest = git_repo.parent / "sequence.yaml"
    manifest.write_text(yaml.safe_dump(payload))
    options = SequencePrepareOptions(
        repo_path=git_repo, manifest_path=manifest, config_path=config_path, **scheduler_paths
    )
    result = prepare_sequence(options)
    assert prepare_sequence(options).reused_existing
    seq = sequence_state(scheduler_paths, result.sequence_id)
    assert seq.definition.entries[0].implementer.model == "gpt-5.6-sol"
    assert seq.definition.entries[0].implementer.reasoning_effort == "xhigh"
    assert seq.definition.entries[0].implementer.timeout_minutes == 41
    assert seq.definition.entries[1].implementer.provider == "cursor"
    validate_schema(seq.model_dump(mode="json"), "scheduler-prepared-sequence-v1.json")
    config_path.write_text("broken source after prepare")
    manifest.write_text("broken manifest after prepare")
    start_sequence(result.sequence_id, db_path=scheduler_paths["db_path"])
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    final = drive(
        _tick(git_repo, scheduler_paths), scheduler_paths, result.sequence_id, monkeypatch
    )
    assert final.definition == seq.definition
    for entry in final.materialized_entries:
        validate_schema(
            leaf_state(scheduler_paths, entry.run_id).context.model_dump(mode="json"),
            "scheduler-submitted-run-context-v6.json",
        )


@pytest.mark.parametrize(
    "override",
    [{"cursor": {"model": "grok-4.7-high"}}, {"workflow": {"cursor_timeout_minutes": 2}}],
)
def test_prepare_rejects_conflicting_cursor_overrides(git_repo, scheduler_paths, override):
    payload = manifest_payload(("codex", "cursor"))
    payload["phases"][0].update(override)
    with pytest.raises(ValidationError, match="Cursor overrides conflict"):
        prepare(git_repo, scheduler_paths, ("codex", "cursor"), payload=payload)


def test_v1_stays_cursor_and_rejects_implementer_metadata(git_repo, scheduler_paths, monkeypatch):
    payload = manifest_payload(("codex", "cursor"), version=1)
    with pytest.raises(ModelValidationError):
        SequenceManifest.model_validate(payload)
    del monkeypatch
    with pytest.raises(ValidationError, match="requires manifest v3"):
        prepare(git_repo, scheduler_paths, ("cursor", "cursor"), version=1)


def test_abort_uncertain_capacity_continuation_holds_then_converges(
    git_repo, scheduler_paths, monkeypatch
):
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    result = prepare(git_repo, scheduler_paths, ("codex", "cursor"))
    start_sequence(result.sequence_id, db_path=scheduler_paths["db_path"])
    waiting = drive(
        _tick(git_repo, scheduler_paths),
        scheduler_paths,
        result.sequence_id,
        monkeypatch,
        target="waiting_codex_capacity",
    )
    scenario = FakeAttemptScenario(active_ticks=100, stays_active_after_terminate=True)
    backend = FakeAgentProcessBackend(default_scenario=scenario)
    restarted = _tick(git_repo, scheduler_paths, backend=backend)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    for _ in range(4):
        restarted.run_once()
        if backend.launch_calls:
            break
    assert len(backend.launch_calls) == 1
    abort = SchedulerAbortService(restarted.store, backend, artifacts=restarted.artifacts)
    receipt = SequenceAbortService(restarted.store, run_abort=abort).abort_sequence(
        result.sequence_id
    )
    assert receipt.run_termination_pending
    with restarted.store.begin_read() as conn:
        assert restarted.store.get_reservation_for_run(conn, waiting.run_id) is not None
    assert len(sequence_state(scheduler_paths, result.sequence_id).materialized_entries) == 1
    # Owned OS unit survives a service restart; no new dispatch is authorized.
    replay = _tick(git_repo, scheduler_paths, backend=backend)
    replay.run_once()
    assert len(backend.launch_calls) == 1
    scenario.active_ticks = 0
    scenario.stays_active_after_terminate = False
    for _ in range(8):
        replay.run_once()
    seq = sequence_state(scheduler_paths, result.sequence_id)
    assert seq.aborted_at and len(seq.materialized_entries) == 1
    with replay.store.begin_read() as conn:
        assert replay.store.get_reservation_for_run(conn, waiting.run_id) is None
    assert len(backend.launch_calls) == 1
    assert (git_repo / "codex-phase-1.txt").is_file()


def test_genuine_accepted_v1_frozen_fixture_preserves_hashes_and_materializes(tmp_path):
    import shutil

    from ai_dev_loop.scheduler.application.sequence_materializer import SequenceRunMaterializer
    from ai_dev_loop.scheduler.domain.sequence import PreparedSequenceState

    fixture = Path(__file__).parents[1] / "fixtures/phase27_3_historical"
    state = PreparedSequenceState.model_validate_json((fixture / "prepared.json").read_text())
    original_hashes = json.loads((fixture / "entry-hashes.json").read_text())
    assert [frozen_entry_hash(e) for e in state.definition.entries] == original_hashes
    root = tmp_path / "artifacts"
    shutil.copytree(fixture / "artifacts", root)
    # Git records executable bits, not the private 0700/0600 modes of the
    # accepted protected-artifact capture. Restore those modes on the owned copy.
    for path in root.rglob("*"):
        path.chmod(DIR_MODE if path.is_dir() else SENSITIVE_FILE_MODE)
    artifacts = ProtectedArtifactStore(root)
    artifacts.verify_prepared_sequence_artifacts(state.sequence_id, state.definition)
    materializer = SequenceRunMaterializer(artifacts)
    for index, entry in enumerate(state.definition.entries):
        method = (
            materializer.materialize_first_entry
            if index == 0
            else materializer.materialize_next_entry
        )
        context, digest = method(
            sequence_id=state.sequence_id, definition=state.definition, entry=entry
        )
        assert digest == original_hashes[index]
        assert context.schema_version == 4 and context.implementer is None
        validate_schema(context.model_dump(mode="json"), "scheduler-submitted-run-context-v4.json")


def test_unsafe_identity_sequence_hold_survives_restart_until_explicit_abort(
    git_repo, scheduler_paths, monkeypatch
):
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_MODE", "conflicting_identity")
    result = prepare(git_repo, scheduler_paths, ("codex", "cursor"))
    start = start_sequence(result.sequence_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    for _ in range(40):
        tick.run_once()
        state = leaf_state(scheduler_paths, start.run_id)
        if state.kind == "blocked":
            break
    assert (
        state.kind == "blocked"
        and state.block_reason_kind == "codex_implementer_identity_uncertain"
    )
    with tick.store.begin_read() as conn:
        assert tick.store.get_reservation_for_run(conn, start.run_id) is not None
    restarted = _tick(git_repo, scheduler_paths)
    restarted.run_once()
    seq = sequence_state(scheduler_paths, result.sequence_id)
    assert len(seq.materialized_entries) == 1
    with restarted.store.begin_read() as conn:
        assert restarted.store.get_reservation_for_run(conn, start.run_id) is not None
    SequenceAbortService(restarted.store).abort_sequence(result.sequence_id)
    for _ in range(6):
        restarted.run_once()
    seq = sequence_state(scheduler_paths, result.sequence_id)
    assert seq.aborted_at and len(seq.materialized_entries) == 1
    assert leaf_state(scheduler_paths, start.run_id).kind == "aborted"
    with restarted.store.begin_read() as conn:
        assert restarted.store.get_reservation_for_run(conn, start.run_id) is None


@pytest.mark.parametrize("kind", ["standalone", "sequence"])
def test_cli_smoke_real_admission_fake_clis_and_frozen_models(
    git_repo, scheduler_paths, monkeypatch, kind
):
    from typer.testing import CliRunner

    import ai_dev_loop.cli as cli
    from ai_dev_loop.cli import app

    runner = CliRunner()
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "cli-implementation.txt")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    if kind == "standalone":
        submitted = runner.invoke(
            app,
            [
                "scheduler",
                "submit",
                "--short-name",
                "CLI smoke",
                "--repo-path",
                str(git_repo),
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
                "--codex-review-model",
                "gpt-6.1-sol",
                "--codex-review-reasoning-effort",
                "high",
                "--output",
                "json",
            ],
            input=(git_repo / "docs/plans/prompt_sample-plan.txt").read_text(),
        )
        assert submitted.exit_code == 0, submitted.output
        target = json.loads(submitted.stdout)["run_id"]
        start = runner.invoke(app, ["scheduler", "start", target])
    else:
        manifest = git_repo.parent / "cli-sequence.yaml"
        manifest.write_text(yaml.safe_dump(manifest_payload(("codex", "cursor"))))
        submitted = runner.invoke(
            app,
            [
                "scheduler",
                "sequence",
                "prepare",
                "--repo-path",
                str(git_repo),
                "--manifest",
                str(manifest),
                "--output",
                "json",
            ],
        )
        assert submitted.exit_code == 0, submitted.output
        target = json.loads(submitted.stdout)["sequence_id"]
        start = runner.invoke(app, ["scheduler", "sequence", "start", target])
    assert start.exit_code == 0, start.output
    tick = _tick(git_repo, scheduler_paths)
    monkeypatch.setattr(cli, "run_scheduler_tick", tick.run_once)
    for _ in range(100):
        response = runner.invoke(app, ["scheduler", "tick", "--output", "json"])
        assert response.exit_code == 0, response.output
        if kind == "standalone":
            state = leaf_state(scheduler_paths, target)
            if state.kind == "completed":
                break
            assert state.kind != "blocked"
        elif hasattr(sequence_state(scheduler_paths, target), "finalized_at"):
            break
    else:
        raise AssertionError("CLI smoke did not finish")
    assert git(git_repo, "diff", "--cached", "--name-only")
    help_result = runner.invoke(app, ["scheduler", "submit", "--help"])
    assert "not available yet" not in help_result.stdout
    assert "automatic" in help_result.stdout and "deferred" in help_result.stdout


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"])
def test_manifest_v2_preserves_supported_codex_effort_vocabulary(effort):
    payload = manifest_payload(("codex", "cursor"), version=2)
    payload["phases"][0]["implementer"]["reasoning_effort"] = effort
    assert SequenceManifest.model_validate(payload).phases[0].implementer.reasoning_effort == effort
    validate_schema(payload, "scheduler-sequence-manifest-v2.json")


@pytest.mark.parametrize("missing", ["model", "reasoning_effort"])
def test_prepare_requires_resolved_codex_settings_before_publication(
    git_repo, scheduler_paths, missing
):
    payload = manifest_payload(("codex", "cursor"))
    del payload["phases"][0]["implementer"][missing]
    with pytest.raises(ValidationError, match="required"):
        prepare(git_repo, scheduler_paths, ("codex", "cursor"), payload=payload)
    store = SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduler_sequences").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0] == 0


def test_v1_explicit_cursor_config_preserves_canonical_effective_representation(
    git_repo, scheduler_paths
):
    source = git_repo / "ai_dev_loop.yaml"
    source.write_text(source.read_text() + "\nimplementer:\n  provider: cursor\n")
    with pytest.raises(ValidationError, match="requires manifest v3"):
        prepare(git_repo, scheduler_paths, ("cursor", "cursor"), version=1)

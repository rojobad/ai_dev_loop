"""Phase 27.1 implementer input, binding, and dispatch-guard contracts."""

from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import sample_agent_led_submitted_context
from tests.unit.scheduler.test_tick import FakeAgentProcessBackend, FakeGitAdmissionPort
from typer.testing import CliRunner

from ai_dev_loop.cli import app, scheduler_submit_command
from ai_dev_loop.commands.config_cmd import validate_config
from ai_dev_loop.config import (
    ConfigOverrides,
    load_project_config,
    resolve_effective_config,
)
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.paths import global_config_path, schema_path
from ai_dev_loop.runners.probes import (
    CompatibilityClassification,
    probe_codex_model_compatibility,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    SequencePrepareService,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import (
    SubmissionService,
    SubmitOptions,
    _freeze_execution_commands,
    historical_effective_config_sha256,
    submission_idempotency_key_candidates,
    submit_run,
)
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.codex_contract import BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND
from ai_dev_loop.scheduler.domain.cursor_contract import CREATE_CHAT_EFFECT_KIND
from ai_dev_loop.scheduler.domain.implementer import (
    CODEX_IMPLEMENTER_NOT_IMPLEMENTED,
    assert_distinct_implementer_and_reviewer_sessions,
)
from ai_dev_loop.scheduler.domain.state import (
    SUBMITTED_CONTEXT_ADAPTER,
    ImplementerBinding,
    SubmittedRunContext,
)
from ai_dev_loop.scheduler.infrastructure.paths import (
    default_artifact_root,
    default_engine_db_path,
    run_artifact_root,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.repository_target import RepositoryTarget
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_BASE = (
    "version: 1\n"
    "project:\n  name: fixture-project\n"
    "cursor:\n  command: agent\n  model: composer-2.5-fast\n"
    "  output_format: stream-json\n  force: true\n  trust_workspace: true\n"
    "  sandbox: disabled\n"
    "codex:\n  command: codex\n  review_skill: review-staged-cursor-execution\n"
    "  sandbox: workspace-write\n"
    "workflow:\n  max_review_iterations: 3\n  require_clean_worktree: true\n"
    "  stage_mode: all\n  cursor_timeout_minutes: 90\n  codex_timeout_minutes: 90\n"
    "prompt:\n  directory: docs/plans\n  filename_template: prompt_{plan_stem}.txt\n"
)


def _write_config(path: Path, extra: str = "") -> Path:
    path.write_text(_BASE + extra, encoding="utf-8")
    return path


def _submit_options(repo: Path, paths: dict[str, Path], **overrides: object) -> SubmitOptions:
    payload: dict[str, object] = {
        "repo_path": repo,
        "plan_path": Path("docs/plans/sample-plan.md"),
        "prompt_source_path": Path("docs/plans/prompt_sample-plan.txt"),
        "codex_review_model": "gpt-5.6-sol",
        "codex_review_reasoning_effort": "high",
        "db_path": paths["db_path"],
        "artifact_root": paths["artifact_root"],
    }
    payload.update(overrides)
    return SubmitOptions(**payload)  # type: ignore[arg-type]


def _prompt() -> str:
    return (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")


def _load_state(paths: dict[str, Path], run_id: str) -> object:
    store = SqliteSchedulerStore(paths["db_path"])
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
    return state


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    del fake_clis
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_omitted_implementer_stays_cursor(tmp_path: Path) -> None:
    config = load_project_config(_write_config(tmp_path / "ai_dev_loop.yaml"))
    assert config.implementer.provider == "cursor"
    dumped = config.model_dump()
    assert "implementer" not in dumped


def test_explicit_cursor_and_codex_settings(tmp_path: Path) -> None:
    cursor = load_project_config(
        _write_config(tmp_path / "cursor.yaml", "implementer:\n  provider: cursor\n")
    )
    assert cursor.implementer.provider == "cursor"
    assert cursor.model_dump()["implementer"] == {"provider": "cursor"}
    codex = load_project_config(
        _write_config(
            tmp_path / "codex.yaml",
            "implementer:\n  provider: codex\n  model: gpt-5.5\n  reasoning_effort: xhigh\n",
        )
    )
    assert codex.implementer.provider == "codex"
    assert codex.implementer.command == "codex"
    assert codex.implementer.model == "gpt-5.5"
    assert codex.implementer.reasoning_effort == "xhigh"
    assert codex.implementer.timeout_minutes == 90


@pytest.mark.parametrize(
    "extra",
    [
        "implementer:\n  provider: other\n",
        "implementer:\n  provider: cursor\n  model: gpt-5.5\n",
        "implementer: null\n",
        "implementer:\n  provider: codex\n  model: ''\n  reasoning_effort: high\n",
        "implementer:\n  provider: codex\n  model: gpt-5.5\n  reasoning_effort: null\n",
        "implementer:\n  provider: codex\n  model: gpt-5.5\n  reasoning_effort: high\n"
        "  timeout_minutes: true\n",
        "implementer:\n  provider: codex\n  model: gpt-5.5\n  reasoning_effort: high\n"
        "  timeout_minutes: 1.5\n",
        "implementer:\n  provider: codex\n  model: gpt-5.5\n  reasoning_effort: weird\n",
        "implementer:\n  provider: codex\n  model: gpt-5.5\n  reasoning_effort: high\n"
        "  sandbox: danger-full-access\n",
    ],
)
def test_invalid_implementer_configuration_is_rejected(tmp_path: Path, extra: str) -> None:
    with pytest.raises(ValidationError):
        load_project_config(_write_config(tmp_path / "ai_dev_loop.yaml", extra))


def test_submit_help_lists_implementer_flags() -> None:
    result = CliRunner().invoke(app, ["scheduler", "submit", "--help"])
    assert result.exit_code == 0
    parameters = inspect.signature(scheduler_submit_command).parameters
    for name in (
        "implementer_provider",
        "implementer_command",
        "implementer_model",
        "implementer_reasoning_effort",
        "implementer_timeout_minutes",
    ):
        assert name in parameters
    assert "--implementer-provider" in result.stdout
    assert "--implementer-model" in result.stdout


def test_reviewer_fields_stay_independent_of_implementer(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    with patch("sys.stdin", StringIO(_prompt())):
        submitted = submit_run(
            _submit_options(
                git_repo,
                scheduler_paths,
                implementer_provider="codex",
                implementer_model="gpt-5.5",
                implementer_reasoning_effort="xhigh",
            )
        )
    state = _load_state(scheduler_paths, submitted.run_id)
    assert state.context.implementer is not None
    assert state.context.implementer.model == "gpt-5.5"
    assert state.context.implementer.reasoning_effort == "xhigh"
    assert state.context.codex.review_model == "gpt-5.6-sol"
    assert state.context.codex.review_reasoning_effort == "high"
    with pytest.raises(SchedulerEngineError) as caught, patch("sys.stdin", StringIO(_prompt())):
        submit_run(
            _submit_options(
                git_repo,
                scheduler_paths,
                codex_review_model="gpt-5.5",
                implementer_provider="codex",
                implementer_model="gpt-5.5",
                implementer_reasoning_effort="xhigh",
            )
        )
    assert caught.value.kind == SchedulerEngineErrorKind.CONFLICT


def test_cursor_submit_is_schema_5_and_replays(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("scheduler submit must not invoke subprocesses")

    monkeypatch.setattr("ai_dev_loop.process.run_process", forbid_subprocess)
    with patch("sys.stdin", StringIO(_prompt())):
        first = submit_run(_submit_options(git_repo, scheduler_paths))
    with patch("sys.stdin", StringIO(_prompt())):
        replay = submit_run(
            _submit_options(git_repo, scheduler_paths, implementer_provider="cursor")
        )
    assert replay.run_id == first.run_id
    assert replay.reused_existing is True
    state = _load_state(scheduler_paths, first.run_id)
    assert state.context.schema_version == 5
    assert state.context.implementer.provider == "cursor"
    assert state.context.implementer.session_id is None
    root = run_artifact_root(scheduler_paths["artifact_root"], first.run_id)
    effective = yaml.safe_load((root / "effective-config.yaml").read_text(encoding="utf-8"))
    source = yaml.safe_load((root / "source-config.yaml").read_text(encoding="utf-8"))
    assert effective["implementer"] == {"provider": "cursor"}
    assert "implementer" not in source


def test_historical_cursor_key_replays_schema_5_submit(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    with patch("sys.stdin", StringIO(_prompt())):
        submitted = submit_run(_submit_options(git_repo, scheduler_paths))
    state = _load_state(scheduler_paths, submitted.run_id)
    effective, _, _ = resolve_effective_config(
        repo_root=git_repo,
        overrides=ConfigOverrides(
            codex_review_model="gpt-5.6-sol",
            codex_review_reasoning_effort="high",
        ),
    )
    frozen = _freeze_execution_commands(effective)
    historical_sha = historical_effective_config_sha256(frozen)
    keys = submission_idempotency_key_candidates(
        state.context,
        historical_effective_config_sha256=historical_sha,
    )
    assert len(keys) >= 2
    assert keys[0] != keys[1]
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        conn.execute(
            "UPDATE scheduler_runs SET idempotency_key = ? WHERE run_id = ?",
            (keys[1], submitted.run_id),
        )
    with patch("sys.stdin", StringIO(_prompt())):
        replay = submit_run(_submit_options(git_repo, scheduler_paths))
    assert replay.run_id == submitted.run_id
    assert replay.reused_existing is True


def test_codex_submit_does_not_require_cursor_or_reuse_cursor_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (fake_clis["bin_dir"] / "agent").unlink()

    def forbid_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("scheduler submit must not invoke subprocesses")

    monkeypatch.setattr("ai_dev_loop.process.run_process", forbid_subprocess)
    with patch("sys.stdin", StringIO(_prompt())):
        cursor = submit_run(_submit_options(git_repo, scheduler_paths))
    with pytest.raises(SchedulerEngineError) as caught, patch("sys.stdin", StringIO(_prompt())):
        submit_run(
            _submit_options(
                git_repo,
                scheduler_paths,
                implementer_provider="codex",
                implementer_model="gpt-5.5",
                implementer_reasoning_effort="xhigh",
            )
        )
    assert caught.value.kind == SchedulerEngineErrorKind.CONFLICT
    cursor_state = _load_state(scheduler_paths, cursor.run_id)
    codex_binding = ImplementerBinding(
        provider="codex",
        command="/usr/bin/codex",
        model="gpt-5.5",
        reasoning_effort="xhigh",
        timeout_minutes=90,
        sandbox="workspace-write",
        approval_policy="never",
    )
    codex_context = cursor_state.context.model_copy(update={"implementer": codex_binding})
    codex_keys = submission_idempotency_key_candidates(
        codex_context,
        historical_effective_config_sha256="a" * 64,
    )
    historical_keys = submission_idempotency_key_candidates(
        cursor_state.context,
        historical_effective_config_sha256="b" * 64,
    )
    assert set(codex_keys).isdisjoint(set(historical_keys))


def test_codex_submit_without_active_cursor_reservation_freezes_binding(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
) -> None:
    (fake_clis["bin_dir"] / "agent").unlink()
    with patch("sys.stdin", StringIO(_prompt())):
        submitted = submit_run(
            _submit_options(
                git_repo,
                scheduler_paths,
                implementer_provider="codex",
                implementer_model="gpt-5.5",
                implementer_reasoning_effort="xhigh",
            )
        )
    with patch("sys.stdin", StringIO(_prompt())):
        replay = submit_run(
            _submit_options(
                git_repo,
                scheduler_paths,
                implementer_provider="codex",
                implementer_model="gpt-5.5",
                implementer_reasoning_effort="xhigh",
            )
        )
    assert replay.run_id == submitted.run_id
    state = _load_state(scheduler_paths, submitted.run_id)
    binding = state.context.implementer
    assert binding.provider == "codex"
    assert binding.purpose == "implementer"
    assert binding.reasoning_effort == "xhigh"
    assert binding.sandbox == "workspace-write"
    assert binding.approval_policy == "never"
    assert binding.session_id is None
    assert binding.timeout_minutes == 90
    assert Path(binding.command).is_file()
    root = run_artifact_root(scheduler_paths["artifact_root"], submitted.run_id)
    effective = yaml.safe_load((root / "effective-config.yaml").read_text(encoding="utf-8"))
    assert effective["implementer"]["provider"] == "codex"
    assert effective["implementer"]["model"] == "gpt-5.5"
    assert "sandbox" not in effective["implementer"]


def test_cursor_overrides_conflict_with_codex_selection(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    with (
        pytest.raises(ValidationError, match="conflict"),
        patch("sys.stdin", StringIO(_prompt())),
    ):
        submit_run(
            _submit_options(
                git_repo,
                scheduler_paths,
                implementer_provider="codex",
                implementer_model="gpt-5.5",
                implementer_reasoning_effort="high",
                cursor_model="composer-2.5-fast",
            )
        )


def test_schema_5_and_historical_schema_3_alignment() -> None:
    historical = sample_agent_led_submitted_context()
    historical_payload = json.loads(SUBMITTED_CONTEXT_ADAPTER.dump_json(historical))
    assert historical.schema_version == 3
    assert "implementer" not in historical_payload
    schema_v3 = json.loads(
        schema_path("scheduler-submitted-run-context-v3.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(historical_payload, schema_v3)
    round_trip = SubmittedRunContext.model_validate(historical_payload)
    assert round_trip.schema_version == 3
    assert round_trip.implementer is None
    with pytest.raises(PydanticValidationError):
        SubmittedRunContext.model_validate(
            {**historical_payload, "implementer": {"provider": "cursor"}}
        )
    current = historical.model_copy(
        update={
            "schema_version": 5,
            "implementer": ImplementerBinding(
                provider="cursor",
                command="agent",
                model="composer-2.5-fast",
                timeout_minutes=90,
                sandbox="disabled",
            ),
        }
    )
    current_payload = json.loads(SUBMITTED_CONTEXT_ADAPTER.dump_json(current))
    schema_v5 = json.loads(
        schema_path("scheduler-submitted-run-context-v5.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(current_payload, schema_v5)
    review = json.loads(schema_path("codex-review-result-v1.json").read_text(encoding="utf-8"))
    assert "cursor_fix_prompt" in review["required"]
    with pytest.raises(ValueError, match="distinct"):
        assert_distinct_implementer_and_reviewer_sessions(
            "11111111-1111-1111-1111-111111111111",
            "11111111-1111-1111-1111-111111111111",
        )


def test_effort_probe_uses_exact_catalog_evidence(fake_clis: dict[str, Path]) -> None:
    catalog = fake_clis["codex_models_file"]
    catalog.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "gpt-5.5",
                        "supported_reasoning_efforts": ["high", "xhigh"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    command = str(fake_clis["bin_dir"] / "codex")
    listed = probe_codex_model_compatibility(
        command,
        required_model="gpt-5.5",
        required_reasoning_effort="xhigh",
    )
    assert listed.classification == CompatibilityClassification.COMPATIBLE
    assert listed.required_model == "gpt-5.5"
    missing_effort = probe_codex_model_compatibility(
        command,
        required_model="gpt-5.5",
        required_reasoning_effort="minimal",
    )
    assert missing_effort.classification == CompatibilityClassification.INCOMPATIBLE_MODEL
    assert missing_effort.required_model == "gpt-5.5"
    catalog.write_text(json.dumps({"models": [{"id": "gpt-5.5"}]}), encoding="utf-8")
    unknown = probe_codex_model_compatibility(
        command,
        required_model="gpt-5.5",
        required_reasoning_effort="xhigh",
    )
    assert unknown.classification == CompatibilityClassification.UNKNOWN
    absent = probe_codex_model_compatibility(
        command,
        required_model="missing-model",
        required_reasoning_effort="xhigh",
    )
    assert absent.classification == CompatibilityClassification.INCOMPATIBLE_MODEL
    assert absent.required_model == "missing-model"
    unchanged = probe_codex_model_compatibility(command, required_model="gpt-5.5")
    assert unchanged.classification == CompatibilityClassification.COMPATIBLE


@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        (
            [{"effort": "high"}, {"effort": "xhigh", "description": "Extra reasoning"}],
            CompatibilityClassification.COMPATIBLE,
        ),
        ([{"effort": "high"}], CompatibilityClassification.INCOMPATIBLE_MODEL),
        (
            [{"effort": "xhigh"}, {"description": "missing effort"}],
            CompatibilityClassification.UNKNOWN,
        ),
        ([{"effort": None}], CompatibilityClassification.UNKNOWN),
        ([{"effort": " "}], CompatibilityClassification.UNKNOWN),
        ("xhigh", CompatibilityClassification.UNKNOWN),
        ([], CompatibilityClassification.INCOMPATIBLE_MODEL),
    ],
)
def test_native_codex_reasoning_levels_catalog(
    fake_clis: dict[str, Path], levels: object, expected: CompatibilityClassification
) -> None:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-5.6-sol", "supported_reasoning_levels": levels},
                    {"slug": "gpt-6.1-sol", "supported_reasoning_levels": [{"effort": "high"}]},
                ]
            }
        ),
        encoding="utf-8",
    )
    command = str(fake_clis["bin_dir"] / "codex")
    result = probe_codex_model_compatibility(
        command, required_model="gpt-5.6-sol", required_reasoning_effort="xhigh"
    )
    assert result.classification == expected
    reviewer = probe_codex_model_compatibility(
        command, required_model="gpt-6.1-sol", required_reasoning_effort="high"
    )
    assert reviewer.classification == CompatibilityClassification.COMPATIBLE


def _tick_until(
    paths: dict[str, Path],
    repo: Path,
    backend: FakeAgentProcessBackend,
    *,
    count: int = 8,
) -> None:
    store = SqliteSchedulerStore(paths["db_path"])
    tick = TickService(
        store,
        ProtectedArtifactStore(paths["artifact_root"]),
        FakeGitAdmissionPort(resolved_root=str(repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
        tick_owner_factory=lambda: "tick-phase-27-1",
        attempt_backend=backend,
    )
    for _ in range(count):
        tick.run_once()


def test_codex_preflight_schedules_implementer_without_cursor_chat(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
) -> None:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}],
                    },
                    {
                        "slug": "gpt-5.5",
                        "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (fake_clis["bin_dir"] / "agent").unlink()
    with patch("sys.stdin", StringIO(_prompt())):
        submitted = submit_run(
            _submit_options(
                git_repo,
                scheduler_paths,
                implementer_provider="codex",
                implementer_model="gpt-5.5",
                implementer_reasoning_effort="xhigh",
            )
        )
    start_run(submitted.run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend()
    _tick_until(scheduler_paths, git_repo, backend, count=1)
    state = _load_state(scheduler_paths, submitted.run_id)
    assert state.kind == "preflight_complete"
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        kinds = [
            str(row["effect_kind"])
            for row in conn.execute(
                "SELECT effect_kind FROM scheduler_effects WHERE run_id = ?",
                (submitted.run_id,),
            )
        ]
    assert CREATE_CHAT_EFFECT_KIND not in kinds
    assert BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND in kinds
    assert backend.launch_calls == []
    agent_log = (
        fake_clis["agent_log"].read_text(encoding="utf-8")
        if fake_clis["agent_log"].exists()
        else ""
    )
    codex_log = (
        fake_clis["codex_log"].read_text(encoding="utf-8")
        if fake_clis["codex_log"].exists()
        else ""
    )
    assert "CREATE_CHAT" not in agent_log
    assert "ARGS:" not in codex_log


def test_ambiguous_effort_catalog_blocks_before_dispatch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
) -> None:
    (fake_clis["bin_dir"] / "agent").unlink()
    with patch("sys.stdin", StringIO(_prompt())):
        submitted = submit_run(
            _submit_options(
                git_repo,
                scheduler_paths,
                implementer_provider="codex",
                implementer_model="gpt-5.5",
                implementer_reasoning_effort="xhigh",
            )
        )
    start_run(submitted.run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend()
    _tick_until(scheduler_paths, git_repo, backend)
    state = _load_state(scheduler_paths, submitted.run_id)
    assert state.kind == "blocked"
    assert state.block_reason_kind == "tool_compatibility_unknown"
    assert backend.launch_calls == []


def test_cursor_preflight_still_schedules_create_chat(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
) -> None:
    with patch("sys.stdin", StringIO(_prompt())):
        submitted = submit_run(_submit_options(git_repo, scheduler_paths))
    start_run(submitted.run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend()
    _tick_until(scheduler_paths, git_repo, backend)
    state = _load_state(scheduler_paths, submitted.run_id)
    assert state.kind != "blocked" or state.block_reason_kind != CODEX_IMPLEMENTER_NOT_IMPLEMENTED
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        kinds = [
            str(row["effect_kind"])
            for row in conn.execute(
                "SELECT effect_kind FROM scheduler_effects WHERE run_id = ?",
                (submitted.run_id,),
            )
        ]
    assert CREATE_CHAT_EFFECT_KIND in kinds
    assert "CREATE_CHAT" in fake_clis["agent_log"].read_text(encoding="utf-8")


def test_sequence_v1_rejects_codex_and_keeps_omitted_cursor(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    manifest = git_repo / "sequence.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": "fixture-sequence",
                "phases": [
                    {
                        "name": "phase-one",
                        "plan_path": "docs/plans/sample-plan.md",
                        "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                        "commit_message": "checkpoint after phase one",
                        "codex": {
                            "review_model": "gpt-5.6-sol",
                            "review_reasoning_effort": "high",
                        },
                    },
                    {
                        "name": "phase-two",
                        "plan_path": "docs/plans/sample-plan.md",
                        "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                        "codex": {
                            "review_model": "gpt-5.6-sol",
                            "review_reasoning_effort": "high",
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    service = SequencePrepareService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        repository_discoverer=lambda _path: RepositoryTarget(root=git_repo.resolve()),
    )
    options = SequencePrepareOptions(
        manifest_path=manifest,
        repo_path=git_repo,
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    prepared = service.prepare(options)
    from ai_dev_loop.scheduler.infrastructure.paths import sequence_artifact_root

    effective_path = (
        sequence_artifact_root(scheduler_paths["artifact_root"], prepared.sequence_id)
        / "entries/01/effective-config.yaml"
    )
    effective = yaml.safe_load(effective_path.read_text(encoding="utf-8"))
    assert "implementer" not in effective
    config_path = git_repo / "ai_dev_loop.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "implementer:\n  provider: codex\n  model: gpt-5.5\n  reasoning_effort: high\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="Cursor-only"):
        service.prepare(options)


def test_config_validate_reports_implementer_provider(git_repo: Path) -> None:
    text = validate_config(repo_path=git_repo, output="text")
    payload = json.loads(validate_config(repo_path=git_repo, output="json"))
    assert "Implementer provider: cursor" in text
    assert payload["implementer_provider"] == "cursor"
    overrides = ConfigOverrides(implementer_provider="cursor")
    effective, _, _ = resolve_effective_config(repo_root=git_repo, overrides=overrides)
    omitted, _, _ = resolve_effective_config(repo_root=git_repo)
    assert effective.implementer.provider == omitted.implementer.provider == "cursor"


_CODEX_IMPLEMENTER = (
    "implementer:\n  provider: codex\n  model: gpt-5.5\n  reasoning_effort: xhigh\n"
)


def _write_global_codex_implementer() -> None:
    path = global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_BASE + _CODEX_IMPLEMENTER, encoding="utf-8")


def _assert_no_published_run(db_path: Path, artifact_root: Path) -> None:
    if db_path.exists():
        with sqlite3.connect(db_path) as conn:
            table = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='scheduler_runs'"
            ).fetchone()
            if table is not None and table[0]:
                count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
                assert count is not None
                assert count[0] == 0
    assert not (artifact_root / "runs").exists()


def test_repo_cursor_overrides_global_codex_implementer(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    isolated_xdg: Path,
) -> None:
    del isolated_xdg
    _write_global_codex_implementer()
    config_path = git_repo / "ai_dev_loop.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "implementer:\n  provider: cursor\n",
        encoding="utf-8",
    )
    effective, source, _ = resolve_effective_config(repo_root=git_repo)
    assert source.implementer.provider == "cursor"
    assert effective.implementer.provider == "cursor"
    assert effective.implementer.model is None
    with patch("sys.stdin", StringIO(_prompt())):
        submitted = submit_run(_submit_options(git_repo, scheduler_paths))
    state = _load_state(scheduler_paths, submitted.run_id)
    binding = state.context.implementer
    assert binding.provider == "cursor"
    assert binding.model == "composer-2.5-fast"
    assert binding.reasoning_effort is None
    assert binding.approval_policy is None
    assert binding.command.endswith("agent")


def test_empty_implementer_mapping_overrides_global_codex(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    isolated_xdg: Path,
) -> None:
    del isolated_xdg
    _write_global_codex_implementer()
    config_path = git_repo / "ai_dev_loop.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "implementer: {}\n", encoding="utf-8"
    )
    effective, source, _ = resolve_effective_config(repo_root=git_repo)
    assert source.implementer.provider == "cursor"
    assert source.implementer.model is None
    assert effective.implementer.provider == "cursor"
    assert effective.implementer.model is None
    assert effective.implementer.reasoning_effort is None
    assert effective.implementer.command is None
    service = SubmissionService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        repository_discoverer=lambda _path: RepositoryTarget(root=git_repo.resolve()),
    )
    with patch("sys.stdin", StringIO(_prompt())):
        submitted = service.submit(_submit_options(git_repo, scheduler_paths))
    state = _load_state(scheduler_paths, submitted.run_id)
    binding = state.context.implementer
    assert binding.provider == "cursor"
    assert binding.model == "composer-2.5-fast"
    assert binding.reasoning_effort is None
    assert binding.approval_policy is None
    assert binding.timeout_minutes == 90
    assert binding.command.endswith("agent")
    root = run_artifact_root(scheduler_paths["artifact_root"], submitted.run_id)
    effective_yaml = yaml.safe_load((root / "effective-config.yaml").read_text(encoding="utf-8"))
    assert effective_yaml["implementer"] == {"provider": "cursor"}


def test_cli_cursor_provider_drops_repository_codex_fields(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    config_path = git_repo / "ai_dev_loop.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + _CODEX_IMPLEMENTER, encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="provider cursor"):
        resolve_effective_config(
            repo_root=git_repo,
            overrides=ConfigOverrides(
                implementer_provider="cursor",
                implementer_model="gpt-5.5",
            ),
        )
    effective, _, _ = resolve_effective_config(
        repo_root=git_repo,
        overrides=ConfigOverrides(implementer_provider="cursor"),
    )
    assert effective.implementer.provider == "cursor"
    assert effective.implementer.model is None
    assert effective.implementer.reasoning_effort is None
    with patch("sys.stdin", StringIO(_prompt())):
        submitted = submit_run(
            _submit_options(git_repo, scheduler_paths, implementer_provider="cursor")
        )
    state = _load_state(scheduler_paths, submitted.run_id)
    binding = state.context.implementer
    assert binding.provider == "cursor"
    assert binding.model == "composer-2.5-fast"
    assert binding.reasoning_effort is None
    assert binding.timeout_minutes == 90
    root = run_artifact_root(scheduler_paths["artifact_root"], submitted.run_id)
    source = yaml.safe_load((root / "source-config.yaml").read_text(encoding="utf-8"))
    assert source["implementer"]["provider"] == "codex"
    effective_yaml = yaml.safe_load((root / "effective-config.yaml").read_text(encoding="utf-8"))
    assert effective_yaml["implementer"] == {"provider": "cursor"}


def test_cursor_timeout_conflicts_with_codex_implementer_before_publication(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    isolated_xdg: Path,
) -> None:
    del isolated_xdg
    with (
        pytest.raises(ValidationError, match="--cursor-timeout-minutes"),
        patch("sys.stdin", StringIO(_prompt())),
    ):
        submit_run(
            _submit_options(
                git_repo,
                scheduler_paths,
                implementer_provider="codex",
                implementer_model="gpt-5.5",
                implementer_reasoning_effort="high",
                cursor_timeout_minutes=15,
            )
        )
    _assert_no_published_run(scheduler_paths["db_path"], scheduler_paths["artifact_root"])

    config_path = git_repo / "ai_dev_loop.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + _CODEX_IMPLEMENTER, encoding="utf-8"
    )
    with (
        pytest.raises(ValidationError, match="--cursor-timeout-minutes"),
        patch("sys.stdin", StringIO(_prompt())),
    ):
        submit_run(_submit_options(git_repo, scheduler_paths, cursor_timeout_minutes=15))
    _assert_no_published_run(scheduler_paths["db_path"], scheduler_paths["artifact_root"])

    cli = CliRunner().invoke(
        app,
        [
            "scheduler",
            "submit",
            "--repo-path",
            str(git_repo),
            "--plan-path",
            "docs/plans/sample-plan.md",
            "--prompt-source-path",
            "docs/plans/prompt_sample-plan.txt",
            "--codex-review-model",
            "gpt-5.6-sol",
            "--codex-review-reasoning-effort",
            "high",
            "--cursor-timeout-minutes",
            "15",
        ],
        input=_prompt(),
    )
    assert cli.exit_code == 4
    assert "--cursor-timeout-minutes" in (cli.stdout + cli.stderr)
    _assert_no_published_run(default_engine_db_path(), default_artifact_root())


def test_cursor_selection_accepts_cursor_timeout(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    with patch("sys.stdin", StringIO(_prompt())):
        cursor = submit_run(_submit_options(git_repo, scheduler_paths, cursor_timeout_minutes=15))
    cursor_state = _load_state(scheduler_paths, cursor.run_id)
    assert cursor_state.context.implementer.provider == "cursor"
    assert cursor_state.context.implementer.timeout_minutes == 15
    assert cursor_state.context.workflow.cursor_timeout_minutes == 15


def test_codex_implementer_timeout_is_independent_of_reviewer_timeout(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    with patch("sys.stdin", StringIO(_prompt())):
        codex = submit_run(
            _submit_options(
                git_repo,
                scheduler_paths,
                implementer_provider="codex",
                implementer_model="gpt-5.5",
                implementer_reasoning_effort="xhigh",
                implementer_timeout_minutes=45,
                codex_timeout_minutes=20,
            )
        )
    codex_state = _load_state(scheduler_paths, codex.run_id)
    assert codex_state.context.implementer.provider == "codex"
    assert codex_state.context.implementer.timeout_minutes == 45
    assert codex_state.context.workflow.codex_timeout_minutes == 20
    assert codex_state.context.workflow.cursor_timeout_minutes == 90

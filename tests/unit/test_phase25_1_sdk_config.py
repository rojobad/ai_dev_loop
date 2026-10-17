"""Phase 25.1 config-v2 and temporary dispatch-boundary contracts."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from ai_dev_loop.commands.config_cmd import validate_config
from ai_dev_loop.config import (
    ConfigOverrides,
    CursorSdkSection,
    default_config_dict,
    load_project_config,
    resolve_effective_config,
)
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.paths import global_config_path
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    SequencePrepareService,
)
from ai_dev_loop.scheduler.application.submission import SubmissionService, SubmitOptions
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.repository_target import RepositoryTarget
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _v2_config(*, implementer: str = "") -> str:
    return (
        "version: 2\n"
        "project:\n  name: fixture-project\n"
        "cursor:\n"
        "  runtime: sdk-local\n"
        "  sdk_version: 1.0.37\n"
        "  model: grok-4.7\n"
        "  parameters:\n"
        "    reasoning_effort: high\n"
        "    fast: false\n"
        "    context: 256k\n"
        "  credential_ref: cursor-default\n"
        "  setting_sources: [project]\n"
        "  sandbox: disabled\n"
        "codex:\n"
        "  command: codex\n"
        "  review_skill: review-staged-ai-dev-loop-execution\n"
        "  sandbox: workspace-write\n"
        f"{implementer}"
        "workflow:\n"
        "  max_review_iterations: 7\n"
        "  require_clean_worktree: true\n"
        "  stage_mode: all\n"
        "  cursor_timeout_minutes: 91\n"
        "  codex_timeout_minutes: 92\n"
        "prompt:\n"
        "  directory: docs/plans\n"
        "  filename_template: prompt_{plan_stem}.txt\n"
    )


_CODEX_IMPLEMENTER = (
    "implementer:\n"
    "  provider: codex\n"
    "  command: codex\n"
    "  model: gpt-5.6-sol\n"
    "  reasoning_effort: xhigh\n"
    "  timeout_minutes: 93\n"
)


def _write_v2(repo: Path, *, implementer: str = "") -> Path:
    path = repo / "ai_dev_loop.yaml"
    path.write_text(_v2_config(implementer=implementer), encoding="utf-8")
    return path


def _repository_target(repo: Path) -> RepositoryTarget:
    return RepositoryTarget(root=repo.resolve())


def _paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _submit_options(repo: Path, paths: dict[str, Path]) -> SubmitOptions:
    return SubmitOptions(
        short_name="SDK config test",
        repo_path=repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        codex_review_model="gpt-6.1-sol",
        codex_review_reasoning_effort="high",
        db_path=paths["db_path"],
        artifact_root=paths["artifact_root"],
    )


def _prompt(repo: Path) -> str:
    return (repo / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")


def test_v2_selection_is_explicit_canonical_and_preserves_false(tmp_path: Path) -> None:
    path = tmp_path / "sdk.yaml"
    path.write_text(_v2_config(), encoding="utf-8")
    config = load_project_config(path)
    assert config.version == 2
    assert isinstance(config.cursor, CursorSdkSection)
    assert config.cursor.parameters == {
        "context": "256k",
        "fast": False,
        "reasoning_effort": "high",
    }
    assert config.cursor.wire_parameters() == (
        ("context", "256k"),
        ("fast", "false"),
        ("reasoning_effort", "high"),
    )
    dumped = config.model_dump(mode="json")
    assert dumped["cursor"]["parameters"]["fast"] is False
    assert "command" not in dumped["cursor"]
    assert "output_format" not in dumped["cursor"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("runtime: sdk-local", "runtime: cli"),
        ("sdk_version: 1.0.37", "sdk_version: 1.0.38"),
        ("model: grok-4.7", "model: null"),
        ("fast: false", "fast: 'false'"),
        ("context: 256k", "context: false"),
        ("reasoning_effort: high", "reasoning_effort: 7"),
        ("    context: 256k", "    context: 256k\n    temperature: 0.2"),
        ("  sandbox: disabled", "  sandbox: true"),
        ("  credential_ref: cursor-default", "  credential_ref: ../secret"),
        ("  setting_sources: [project]", "  setting_sources: []"),
        ("  setting_sources: [project]", "  setting_sources: [project, project]"),
        ("  sandbox: disabled", "  sandbox: disabled\n  command: agent"),
        ("  sandbox: disabled", "  sandbox: disabled\n  unknown: value"),
    ],
)
def test_v2_rejects_incomplete_coerced_unknown_and_cli_fields(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text(_v2_config().replace(old, new), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_project_config(path)


def test_v2_rejects_absent_required_and_duplicate_settings(tmp_path: Path) -> None:
    absent = tmp_path / "absent.yaml"
    absent.write_text(
        _v2_config().replace("  credential_ref: cursor-default\n", ""),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="credential_ref"):
        load_project_config(absent)

    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(
        _v2_config().replace("    fast: false\n", "    fast: false\n    fast: true\n"),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="duplicate key"):
        load_project_config(duplicate)


def test_version_specific_defaults_and_cli_overrides_do_not_leak_into_v2(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    del isolated_xdg
    _write_v2(git_repo)
    defaults = default_config_dict(version=2)
    assert defaults["version"] == 2
    assert "cursor" not in defaults
    effective, _, _ = resolve_effective_config(repo_root=git_repo)
    assert isinstance(effective.cursor, CursorSdkSection)
    with pytest.raises(ValidationError, match="not valid for config version 2"):
        resolve_effective_config(
            repo_root=git_repo,
            overrides=ConfigOverrides(cursor_model="legacy-alias"),
        )


def test_provider_change_between_v2_layers_replaces_incompatible_fields(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    del isolated_xdg
    global_path = global_config_path()
    global_path.parent.mkdir(parents=True, mode=0o700)
    global_path.write_text(_v2_config(implementer=_CODEX_IMPLEMENTER), encoding="utf-8")
    _write_v2(git_repo, implementer="implementer:\n  provider: cursor\n")
    effective, source, _ = resolve_effective_config(repo_root=git_repo)
    assert source.implementer.provider == "cursor"
    assert effective.implementer.provider == "cursor"
    assert effective.implementer.model is None
    assert effective.implementer.command is None


def test_mixed_global_and_repository_config_versions_fail_explicitly(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    del isolated_xdg
    global_path = global_config_path()
    global_path.parent.mkdir(parents=True, mode=0o700)
    global_path.write_text((git_repo / "ai_dev_loop.yaml").read_text(), encoding="utf-8")
    _write_v2(git_repo)
    with pytest.raises(ValidationError, match="versions must match"):
        resolve_effective_config(repo_root=git_repo)


def test_cursor_selected_v2_submit_freezes_phase25_2_sdk_context_without_importing_sdk(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    _write_v2(git_repo)
    paths = _paths(isolated_xdg)
    service = SubmissionService(
        SqliteSchedulerStore(paths["db_path"]),
        ProtectedArtifactStore(paths["artifact_root"]),
        repository_discoverer=lambda _: _repository_target(git_repo),
    )
    with patch("sys.stdin", StringIO(_prompt(git_repo))):
        result = service.submit(_submit_options(git_repo, paths))
    with service.store.begin_read() as conn:
        state, _, _ = service.store.load_validated_snapshot(conn, result.run_id)
    assert state.context.schema_version == 7
    assert state.context.cursor.runtime == "sdk-local"  # type: ignore[union-attr]
    assert (service.artifacts.run_root(result.run_id) / "effective-config.yaml").is_file()
    assert importlib.util.find_spec("cursor_sdk") is None


def test_codex_selected_v2_submit_needs_no_cursor_runtime_key_or_extra(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (fake_clis["bin_dir"] / "agent").unlink()
    monkeypatch.setenv("PATH", str(fake_clis["bin_dir"]))
    assert shutil.which("agent") is None
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    _write_v2(git_repo, implementer=_CODEX_IMPLEMENTER)
    paths = _paths(isolated_xdg)
    service = SubmissionService(
        SqliteSchedulerStore(paths["db_path"]),
        ProtectedArtifactStore(paths["artifact_root"]),
        repository_discoverer=lambda _: _repository_target(git_repo),
    )
    with patch("sys.stdin", StringIO(_prompt(git_repo))):
        result = service.submit(_submit_options(git_repo, paths))
    with SqliteSchedulerStore(paths["db_path"]).begin_read() as conn:
        state, _, _ = SqliteSchedulerStore(paths["db_path"]).load_validated_snapshot(
            conn, result.run_id
        )
    assert state.context.implementer.provider == "codex"
    assert state.context.implementer.model == "gpt-5.6-sol"
    assert state.context.implementer.reasoning_effort == "xhigh"
    assert state.context.implementer.timeout_minutes == 93
    assert state.context.codex.review_model == "gpt-6.1-sol"
    assert state.context.codex.review_reasoning_effort == "high"
    effective_path = paths["artifact_root"] / "runs"
    effective_matches = list(effective_path.rglob("effective-config.yaml"))
    assert len(effective_matches) == 1
    effective_payload = yaml.safe_load(effective_matches[0].read_text(encoding="utf-8"))
    assert effective_payload["version"] == 2
    assert effective_payload["cursor"]["parameters"]["fast"] is False
    assert "command" not in effective_payload["cursor"]
    assert importlib.util.find_spec("cursor_sdk") is None


def test_codex_selected_v2_does_not_copy_available_cursor_key_to_scheduler_state(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_clis
    secret = "phase25-artifact-secret-sentinel"
    monkeypatch.setenv("CURSOR_API_KEY", secret)
    credentials = isolated_xdg / "config" / "ai_dev_loop" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    credentials.chmod(0o700)
    credential = credentials / "cursor-default"
    credential.write_text(secret, encoding="utf-8")
    credential.chmod(0o600)
    _write_v2(git_repo, implementer=_CODEX_IMPLEMENTER)
    paths = _paths(isolated_xdg)
    service = SubmissionService(
        SqliteSchedulerStore(paths["db_path"]),
        ProtectedArtifactStore(paths["artifact_root"]),
        repository_discoverer=lambda _: _repository_target(git_repo),
    )
    with patch("sys.stdin", StringIO(_prompt(git_repo))):
        service.submit(_submit_options(git_repo, paths))

    encoded = secret.encode()
    scheduler_state = paths["db_path"].parent
    assert all(
        encoded not in path.read_bytes() for path in scheduler_state.rglob("*") if path.is_file()
    )


def _manifest(*, provider: str) -> str:
    implementer = ""
    if provider == "codex":
        implementer = (
            "    implementer:\n"
            "      provider: codex\n"
            "      command: codex\n"
            "      model: gpt-5.6-sol\n"
            "      reasoning_effort: xhigh\n"
            "      timeout_minutes: 93\n"
        )
    return (
        "schema_version: 3\n"
        "name: SDK config sequence\n"
        "short_name: SDK sequence\n"
        "phases:\n"
        "  - name: one\n"
        "    short_name: One\n"
        "    plan_path: docs/plans/sample-plan.md\n"
        "    prompt_source_path: docs/plans/prompt_sample-plan.txt\n"
        "    commit_message: 'test: one'\n"
        "    codex:\n"
        "      review_model: gpt-6.1-sol\n"
        "      review_reasoning_effort: high\n"
        f"{implementer}"
        "  - name: two\n"
        "    short_name: Two\n"
        "    plan_path: docs/plans/sample-plan.md\n"
        "    prompt_source_path: docs/plans/prompt_sample-plan.txt\n"
        "    codex:\n"
        "      review_model: gpt-6.1-sol\n"
        "      review_reasoning_effort: high\n"
        f"{implementer}"
    )


@pytest.mark.parametrize("provider", ["cursor", "codex"])
def test_v3_prepare_guard_is_cursor_only(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    (fake_clis["bin_dir"] / "agent").unlink()
    monkeypatch.setenv("PATH", str(fake_clis["bin_dir"]))
    assert shutil.which("agent") is None
    _write_v2(git_repo, implementer=_CODEX_IMPLEMENTER if provider == "codex" else "")
    manifest = git_repo / "sequence.yaml"
    manifest.write_text(_manifest(provider=provider), encoding="utf-8")
    paths = _paths(isolated_xdg)
    service = SequencePrepareService(
        SqliteSchedulerStore(paths["db_path"]),
        ProtectedArtifactStore(paths["artifact_root"]),
        repository_discoverer=lambda _: _repository_target(git_repo),
    )
    options = SequencePrepareOptions(
        manifest_path=manifest,
        repo_path=git_repo,
        db_path=paths["db_path"],
        artifact_root=paths["artifact_root"],
    )
    if provider == "cursor":
        with pytest.raises(ValidationError, match="Cursor SDK scheduler dispatch"):
            service.prepare(options)
        with sqlite3.connect(paths["db_path"]) as conn:
            assert conn.execute("SELECT COUNT(*) FROM scheduler_sequences").fetchone()[0] == 0
        assert not list(paths["artifact_root"].rglob("effective-config.yaml"))
    else:
        prepared = service.prepare(options)
        assert prepared.entry_count == 2
        with SqliteSchedulerStore(paths["db_path"]).begin_read() as conn:
            state = SqliteSchedulerStore(paths["db_path"]).load_validated_sequence_state(
                conn, prepared.sequence_id
            )
        assert all(entry.implementer.provider == "codex" for entry in state.definition.entries)
        assert all(entry.implementer.timeout_minutes == 93 for entry in state.definition.entries)
        assert all(entry.codex.review_model == "gpt-6.1-sol" for entry in state.definition.entries)


def test_v2_json_shape_contains_no_secret_value(tmp_path: Path) -> None:
    path = tmp_path / "sdk.yaml"
    path.write_text(_v2_config(), encoding="utf-8")
    payload = json.loads(load_project_config(path).model_dump_json())
    assert payload["cursor"]["credential_ref"] == "cursor-default"
    assert "api_key" not in json.dumps(payload).lower()


def test_config_validate_reports_v2_and_only_warns_for_guarded_cursor(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    del isolated_xdg
    _write_v2(git_repo)
    cursor_text = validate_config(repo_path=git_repo)
    cursor_json = json.loads(validate_config(repo_path=git_repo, output="json"))
    assert cursor_json["config_version"] == 2
    assert "scheduler dispatch remains guarded until Phase 25.2" in cursor_text

    _write_v2(git_repo, implementer=_CODEX_IMPLEMENTER)
    codex_text = validate_config(repo_path=git_repo)
    assert "Implementer provider: codex" in codex_text
    assert "dispatch remains guarded" not in codex_text

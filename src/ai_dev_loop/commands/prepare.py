"""Prepare command implementation."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.config import ConfigOverrides, resolve_effective_config
from ai_dev_loop.errors import UsageError, ValidationError
from ai_dev_loop.event_log import append_orchestrator_event
from ai_dev_loop.fresh_codex_reviewer import (
    FRESH_REVIEWER_INPUT_ARTIFACT,
    build_fresh_reviewer_input_artifact,
    require_frozen_review_model,
    require_frozen_review_reasoning_effort,
)
from ai_dev_loop.integrations.codex.session_runtime import (
    read_codex_session_runtime,
    require_codex_session_id,
)
from ai_dev_loop.paths import ensure_app_dirs, ensure_dir, run_dir, set_sensitive_file_mode
from ai_dev_loop.review_runtime import resolve_effective_review_runtime
from ai_dev_loop.runners.git import (
    GitRepositoryInfo,
    copy_file_atomic,
    discover_repository,
    relative_repo_path,
    resolve_repo_relative_path,
    validate_clean_worktree,
)
from ai_dev_loop.state import (
    RUN_STATE_SCHEMA_VERSION,
    RUN_STATE_SCHEMA_VERSION_FRESH,
    CodexState,
    ControllerState,
    CursorState,
    FreshCodexReviewerBinding,
    ManifestArtifact,
    PlanState,
    ProjectRef,
    PromptState,
    RepositoryState,
    RunManifest,
    RunState,
    RunStatus,
    WorkflowState,
    atomic_write_json,
    atomic_write_text,
    atomic_write_yaml,
    generate_run_id,
    serialize_run_state,
    sha256_bytes,
    sha256_file,
    sha256_text,
    utc_now,
)


@dataclass(frozen=True)
class PrepareOptions:
    config_path: Path | None = None
    project_name: str | None = None
    repo_path: Path | None = None
    plan_path: Path | None = None
    prompt_source_path: Path | None = None
    codex_session_id: str | None = None
    controller_session_id: str | None = None
    cursor_command: str | None = None
    cursor_model: str | None = None
    cursor_output_format: str | None = None
    codex_command: str | None = None
    codex_review_model: str | None = None
    codex_review_reasoning_effort: str | None = None
    review_skill: str | None = None
    max_review_iterations: int | None = None
    cursor_timeout_minutes: int | None = None
    codex_timeout_minutes: int | None = None
    output: str = "text"


@dataclass(frozen=True)
class PrepareResult:
    run_id: str
    project: str
    start_command: str
    launch_command: str | None
    run_directory: Path
    session_model: str
    session_reasoning_effort: str
    review_model: str
    review_reasoning_effort: str
    review_model_source: str
    review_reasoning_source: str
    controller_session_id: str | None = None
    requires_codex_exit: bool = True
    reviewer_must_remain_inactive: bool = False
    model_family_warning: str | None = None
    model_mismatch_warning: str | None = None


def _read_stdin_prompt() -> str:
    if sys.stdin.isatty():
        raise UsageError("prepare requires the exact Cursor prompt on stdin")
    prompt = sys.stdin.read()
    if not prompt.strip():
        raise UsageError("stdin prompt is empty")
    return prompt


def _build_overrides(options: PrepareOptions) -> ConfigOverrides:
    return ConfigOverrides(
        project_name=options.project_name,
        cursor_command=options.cursor_command,
        cursor_model=options.cursor_model,
        cursor_output_format=options.cursor_output_format,
        codex_command=options.codex_command,
        codex_review_model=options.codex_review_model,
        codex_review_reasoning_effort=options.codex_review_reasoning_effort,
        review_skill=options.review_skill,
        max_review_iterations=options.max_review_iterations,
        cursor_timeout_minutes=options.cursor_timeout_minutes,
        codex_timeout_minutes=options.codex_timeout_minutes,
    )


def _resolve_inputs(options: PrepareOptions, repo_info: GitRepositoryInfo) -> tuple[Path, Path]:
    repo_root = repo_info.root
    if options.plan_path is None:
        raise ValidationError("plan path is required (--plan-path)")
    if options.prompt_source_path is None:
        raise ValidationError("prompt source path is required (--prompt-source-path)")
    plan_path = resolve_repo_relative_path(repo_root, options.plan_path)
    prompt_source_path = resolve_repo_relative_path(repo_root, options.prompt_source_path)
    if not plan_path.is_file():
        raise ValidationError(f"plan file not found: {plan_path}")
    if not prompt_source_path.is_file():
        raise ValidationError(f"prompt source file not found: {prompt_source_path}")
    return plan_path, prompt_source_path


def _create_run_layout(base: Path) -> None:
    for relative in (
        "plan",
        "prompts/fixes",
        "cursor/iterations",
        "codex/reviews",
        "codex/events",
        "preflight",
        "logs",
        "locks",
        "git/status",
        "git/diffs",
        "git/cursor-output",
    ):
        ensure_dir(base / relative)


def prepare_run(options: PrepareOptions) -> PrepareResult:
    ensure_app_dirs()
    prompt_text = _read_stdin_prompt()
    repo_candidate = options.repo_path or Path.cwd()
    repo_info = discover_repository(repo_candidate)
    effective, source_repo_config, _repo_config_path = resolve_effective_config(
        repo_root=repo_info.root,
        config_path=options.config_path,
        overrides=_build_overrides(options),
    )
    plan_path, prompt_source_path = _resolve_inputs(options, repo_info)
    validate_clean_worktree(
        repo_info,
        plan_path=plan_path,
        prompt_source_path=prompt_source_path,
        repo_root=repo_info.root,
        require_clean=effective.workflow.require_clean_worktree,
    )

    controller_session_id: str | None = None
    fresh_reviewer: FreshCodexReviewerBinding | None = None
    session_id: str | None = None
    review_runtime = None

    if options.controller_session_id is not None:
        controller_session_id = require_codex_session_id(options.controller_session_id)
        if options.codex_session_id is not None:
            raise ValidationError(
                "controller prepare must not pass --codex-session-id; "
                "reviewer B is created at the first review boundary with frozen "
                "--codex-review-model and --codex-review-reasoning-effort"
            )
        review_model = require_frozen_review_model(options.codex_review_model)
        review_reasoning = require_frozen_review_reasoning_effort(
            options.codex_review_reasoning_effort
        )
        fresh_reviewer = FreshCodexReviewerBinding(
            review_model=review_model,
            review_reasoning_effort=review_reasoning,
        )
    else:
        session_id = require_codex_session_id(options.codex_session_id)
        session_runtime = read_codex_session_runtime(session_id)
        review_runtime = resolve_effective_review_runtime(
            session=session_runtime,
            configured_review_model=effective.codex.review_model,
            configured_review_reasoning_effort=effective.codex.review_reasoning_effort,
        )

    now = utc_now()
    run_id = generate_run_id(effective.project.name, now=now)
    destination = run_dir(effective.project.name, run_id)
    if destination.exists():
        raise ValidationError(f"run directory already exists: {destination}")
    _create_run_layout(destination)

    plan_snapshot = destination / "plan" / "plan.md"
    prompt_snapshot = destination / "prompts" / "cursor-initial.txt"
    copy_file_atomic(plan_path, plan_snapshot)
    atomic_write_text(prompt_snapshot, prompt_text, sensitive=True)

    effective_yaml_path = destination / "effective-config.yaml"
    source_yaml_path = destination / "source-config.yaml"
    atomic_write_yaml(
        effective_yaml_path,
        effective.model_dump(by_alias=True),
        sensitive=True,
    )
    atomic_write_yaml(
        source_yaml_path,
        source_repo_config.model_dump(by_alias=True),
        sensitive=True,
    )

    baseline_status_path = destination / "git" / "baseline-status.txt"
    atomic_write_text(baseline_status_path, repo_info.status_porcelain + "\n")

    plan_hash = sha256_file(plan_snapshot)
    prompt_hash = sha256_text(prompt_text)
    source_config_hash = sha256_file(source_yaml_path)
    effective_config_hash = sha256_bytes(effective_yaml_path.read_bytes())

    manifest_artifacts: list[ManifestArtifact] = [
        ManifestArtifact(path="plan/plan.md", sha256=plan_hash),
        ManifestArtifact(path="prompts/cursor-initial.txt", sha256=prompt_hash),
        ManifestArtifact(path="source-config.yaml", sha256=source_config_hash),
        ManifestArtifact(path="effective-config.yaml", sha256=effective_config_hash),
        ManifestArtifact(path="git/baseline-status.txt", sha256=sha256_file(baseline_status_path)),
    ]

    if fresh_reviewer is not None:
        fresh_input_artifact = build_fresh_reviewer_input_artifact(
            review_model=fresh_reviewer.review_model,
            review_reasoning_effort=fresh_reviewer.review_reasoning_effort,
        )
        fresh_input_path = destination / FRESH_REVIEWER_INPUT_ARTIFACT
        atomic_write_json(fresh_input_path, fresh_input_artifact, sensitive=True)
        manifest_artifacts.append(
            ManifestArtifact(
                path=FRESH_REVIEWER_INPUT_ARTIFACT,
                sha256=sha256_file(fresh_input_path),
            )
        )
        codex_state = CodexState(
            command=effective.codex.command,
            session_id=None,
            session_model=None,
            session_reasoning_effort=None,
            review_model=fresh_reviewer.review_model,
            review_reasoning_effort=fresh_reviewer.review_reasoning_effort,
            review_model_source="explicit",
            review_reasoning_source="explicit",
            model_family_warning=None,
            review_skill=effective.codex.review_skill,
            sandbox=effective.codex.sandbox,
            fresh_reviewer=fresh_reviewer,
        )
        session_model = "(unbound)"
        session_reasoning_effort = "(unbound)"
        review_model = fresh_reviewer.review_model
        review_reasoning_effort = fresh_reviewer.review_reasoning_effort
        review_model_source = "explicit"
        review_reasoning_source = "explicit"
        model_family_warning = None
        model_mismatch_warning = None
    else:
        assert review_runtime is not None
        assert session_id is not None
        session_runtime_artifact = {
            "session_id_prefix": session_id[:8],
            "model": review_runtime.session_model,
            "reasoning_effort": review_runtime.session_reasoning_effort,
            "origin": review_runtime.session_origin,
            "source_event_type": review_runtime.source_event_type,
            "source_timestamp": review_runtime.source_timestamp,
            "review_model": review_runtime.review_model,
            "review_reasoning_effort": review_runtime.review_reasoning_effort,
            "review_model_source": review_runtime.review_model_source,
            "review_reasoning_source": review_runtime.review_reasoning_source,
            "model_mismatch_warning": review_runtime.model_mismatch_warning,
            "model_family_warning": review_runtime.model_family_warning,
        }
        session_runtime_path = destination / "codex" / "session-runtime.json"
        atomic_write_json(session_runtime_path, session_runtime_artifact, sensitive=True)
        manifest_artifacts.append(
            ManifestArtifact(
                path="codex/session-runtime.json",
                sha256=sha256_file(session_runtime_path),
            )
        )
        codex_state = CodexState(
            command=effective.codex.command,
            session_id=session_id,
            session_model=review_runtime.session_model,
            session_reasoning_effort=review_runtime.session_reasoning_effort,
            review_model=review_runtime.review_model,
            review_reasoning_effort=review_runtime.review_reasoning_effort,
            review_model_source=review_runtime.review_model_source,
            review_reasoning_source=review_runtime.review_reasoning_source,
            model_family_warning=review_runtime.model_family_warning,
            review_skill=effective.codex.review_skill,
            sandbox=effective.codex.sandbox,
        )
        session_model = review_runtime.session_model
        session_reasoning_effort = review_runtime.session_reasoning_effort
        review_model = review_runtime.review_model
        review_reasoning_effort = review_runtime.review_reasoning_effort
        review_model_source = review_runtime.review_model_source
        review_reasoning_source = review_runtime.review_reasoning_source
        model_family_warning = review_runtime.model_family_warning
        model_mismatch_warning = review_runtime.model_mismatch_warning

    plan_repo_path = relative_repo_path(repo_info.root, plan_path)
    prompt_repo_path = relative_repo_path(repo_info.root, prompt_source_path)

    state = RunState(
        schema_version=RUN_STATE_SCHEMA_VERSION_FRESH
        if fresh_reviewer is not None
        else RUN_STATE_SCHEMA_VERSION,
        run_id=run_id,
        project=ProjectRef(name=effective.project.name),
        status=RunStatus.PREPARED,
        created_at=now,
        updated_at=now,
        repository=RepositoryState(
            root=str(repo_info.root),
            git_common_dir=str(repo_info.git_common_dir),
            git_dir=str(repo_info.git_dir),
            branch=repo_info.branch,
            initial_head=repo_info.head,
            baseline_status_path="git/baseline-status.txt",
        ),
        plan=PlanState(
            repository_path=plan_repo_path,
            snapshot_path="plan/plan.md",
            sha256=plan_hash,
        ),
        prompt=PromptState(
            source_repository_path=prompt_repo_path,
            snapshot_path="prompts/cursor-initial.txt",
            sha256=prompt_hash,
        ),
        codex=codex_state,
        cursor=CursorState(
            command=effective.cursor.command,
            model=effective.cursor.model,
            output_format=effective.cursor.output_format,
            force=effective.cursor.force,
            trust_workspace=effective.cursor.trust_workspace,
            sandbox=effective.cursor.sandbox,
            chat_id=None,
        ),
        workflow=WorkflowState(
            max_review_iterations=effective.workflow.max_review_iterations,
            current_review_iteration=0,
            stage_mode=effective.workflow.stage_mode,
            cursor_timeout_minutes=effective.workflow.cursor_timeout_minutes,
            codex_timeout_minutes=effective.workflow.codex_timeout_minutes,
        ),
        controller=None
        if controller_session_id is None
        else ControllerState(controller_session_id=controller_session_id),
    )

    manifest = RunManifest(
        run_id=run_id,
        project=effective.project.name,
        created_at=now,
        artifacts=manifest_artifacts,
    )

    plan_metadata = {
        "repository_path": plan_repo_path,
        "snapshot_path": "plan/plan.md",
        "original_filename": plan_path.name,
        "sha256": plan_hash,
    }
    atomic_write_json(destination / "plan" / "metadata.json", plan_metadata)

    atomic_write_json(destination / "state.json", serialize_run_state(state), sensitive=True)
    atomic_write_json(
        destination / "manifest.json",
        json.loads(manifest.model_dump_json(by_alias=True)),
    )

    log_path = destination / "logs" / "ai_dev_loop.log"
    log_lines = [f"{now.isoformat()} prepare completed for run {run_id}"]
    if model_mismatch_warning:
        log_lines.append(model_mismatch_warning)
    if model_family_warning:
        log_lines.append(model_family_warning)
    atomic_write_text(
        log_path,
        "\n".join(log_lines) + "\n",
        sensitive=True,
    )
    append_orchestrator_event(
        destination,
        run_id=run_id,
        component="orchestrator",
        event="prepare_completed",
        status=RunStatus.PREPARED.value,
        detail={
            "project": effective.project.name,
            "review_model_source": review_model_source,
            "review_reasoning_source": review_reasoning_source,
            "has_model_family_warning": model_family_warning is not None,
            "has_controller": controller_session_id is not None,
            "fresh_reviewer_bootstrap": fresh_reviewer is not None,
        },
    )
    set_sensitive_file_mode(destination / "state.json")

    start_command = f"ai_dev_loop start {run_id}"
    is_ab = controller_session_id is not None
    launch_command = (
        f"ai_dev_loop launch {run_id} --controller-session-id {controller_session_id}"
        if is_ab
        else None
    )
    return PrepareResult(
        run_id=run_id,
        project=effective.project.name,
        start_command=start_command,
        launch_command=launch_command,
        run_directory=destination,
        session_model=session_model,
        session_reasoning_effort=session_reasoning_effort,
        review_model=review_model,
        review_reasoning_effort=review_reasoning_effort,
        review_model_source=review_model_source,
        review_reasoning_source=review_reasoning_source,
        controller_session_id=controller_session_id,
        requires_codex_exit=not is_ab,
        reviewer_must_remain_inactive=False,
        model_family_warning=model_family_warning,
        model_mismatch_warning=model_mismatch_warning,
    )


def render_prepare_output(result: PrepareResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "status": "prepared",
            "run_id": result.run_id,
            "project": result.project,
            "start_command": result.start_command,
            "launch_command": result.launch_command,
            "requires_codex_exit": result.requires_codex_exit,
            "reviewer_must_remain_inactive": result.reviewer_must_remain_inactive,
            "controller_session_id_present": result.controller_session_id is not None,
            "session_model": result.session_model,
            "session_reasoning_effort": result.session_reasoning_effort,
            "review_model": result.review_model,
            "review_reasoning_effort": result.review_reasoning_effort,
            "review_model_source": result.review_model_source,
            "review_reasoning_source": result.review_reasoning_source,
            "model_family_warning": result.model_family_warning,
            "model_mismatch_warning": result.model_mismatch_warning,
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Prepared run {result.run_id}",
        f"Project: {result.project}",
        f"State directory: {result.run_directory}",
        f"Session model: {result.session_model}",
        f"Session reasoning: {result.session_reasoning_effort}",
        f"Review model: {result.review_model} ({result.review_model_source})",
        f"Review reasoning: {result.review_reasoning_effort} ({result.review_reasoning_source})",
        f"Start command: {result.start_command}",
    ]
    if result.launch_command:
        lines.append(f"Launch command: {result.launch_command}")
    elif result.controller_session_id is not None:
        lines.append(
            "Controller prepare: reviewer B is created at the first review boundary. "
            "Launch from the controller session when ready."
        )
    else:
        lines.append("Important: exit the active Codex TUI before running start.")
    if result.model_mismatch_warning:
        lines.append(f"Warning: {result.model_mismatch_warning}")
    if result.model_family_warning:
        lines.append(f"Warning: {result.model_family_warning}")
    return "\n".join(lines) + "\n"

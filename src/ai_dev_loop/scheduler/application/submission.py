"""Scheduler submission service."""

from __future__ import annotations

import json
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ai_dev_loop.config import ConfigOverrides, ProjectConfig, resolve_effective_config
from ai_dev_loop.errors import UsageError, ValidationError
from ai_dev_loop.fresh_codex_reviewer import (
    FRESH_REVIEWER_INPUT_ARTIFACT,
    build_fresh_reviewer_input_artifact,
    require_frozen_review_model,
    require_frozen_review_reasoning_effort,
)
from ai_dev_loop.integrations.codex.session_runtime import (
    CodexSessionRuntime,
    read_codex_session_runtime,
    require_codex_session_id,
)
from ai_dev_loop.review_runtime import EffectiveReviewRuntime
from ai_dev_loop.runners.git import (
    GitRepositoryInfo,
    relative_repo_path,
    resolve_repo_relative_path,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    SubmitResult,
    queued_safe_next_action,
)
from ai_dev_loop.scheduler.domain.common import canonical_json_sha256, worktree_key
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.domain.state import (
    SUBMITTED_CONTEXT_SCHEMA_VERSION_FRESH,
    CodexRuntimeBinding,
    ControllerBinding,
    CursorBinding,
    EffectiveConfigBinding,
    FreshCodexReviewerBinding,
    PlanPromptBinding,
    RepositoryBinding,
    SubmittedRunContext,
    SubmittedState,
    WorkflowLimits,
    submission_identity_payload,
)
from ai_dev_loop.scheduler.infrastructure.paths import (
    default_artifact_root,
    default_engine_db_path,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    MAX_BASELINE_STATUS_BYTES,
    MAX_PLAN_BYTES,
    MAX_PROMPT_BYTES,
    MAX_SESSION_RUNTIME_BYTES,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.repository_binding import discover_repository_binding
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import generate_run_id, sha256_bytes, sha256_text, utc_now

PLAN_ARTIFACT = "plan/plan.md"
PROMPT_ARTIFACT = "prompts/cursor-initial.txt"
EFFECTIVE_CONFIG_ARTIFACT = "effective-config.yaml"
SOURCE_CONFIG_ARTIFACT = "source-config.yaml"
BASELINE_STATUS_ARTIFACT = "git/baseline-status.txt"
SESSION_RUNTIME_ARTIFACT = "codex/session-runtime.json"

RepositoryDiscoverer = Callable[[Path], GitRepositoryInfo]
SessionRuntimeReader = Callable[[str], CodexSessionRuntime]


@dataclass(frozen=True)
class SubmitOptions:
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
    db_path: Path | None = None
    artifact_root: Path | None = None


def _read_stdin_prompt() -> str:
    if sys.stdin.isatty():
        raise UsageError("scheduler submit requires the exact Cursor prompt on stdin")
    prompt = sys.stdin.read()
    if not prompt.strip():
        raise UsageError("stdin prompt is empty")
    return prompt


def _build_overrides(options: SubmitOptions) -> ConfigOverrides:
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


def _resolve_inputs(options: SubmitOptions, repo_info: GitRepositoryInfo) -> tuple[Path, Path]:
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


def _session_runtime_artifact_bytes(
    *,
    session_id: str,
    review_runtime: EffectiveReviewRuntime,
) -> bytes:
    payload = {
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
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def _fresh_input_artifact_bytes(
    *,
    review_model: str,
    review_reasoning_effort: str,
) -> bytes:
    payload = build_fresh_reviewer_input_artifact(
        review_model=review_model,
        review_reasoning_effort=review_reasoning_effort,
    )
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def _build_fresh_context(
    *,
    repo_info: GitRepositoryInfo,
    plan_path: Path,
    prompt_source_path: Path,
    effective: ProjectConfig,
    controller_session_id: str,
    review_model: str,
    review_reasoning_effort: str,
    artifact_hashes: dict[str, str],
) -> SubmittedRunContext:
    repo_root = str(repo_info.root)
    return SubmittedRunContext(
        schema_version=SUBMITTED_CONTEXT_SCHEMA_VERSION_FRESH,
        project_name=effective.project.name,
        repository=RepositoryBinding(
            root=repo_root,
            git_common_dir=str(repo_info.git_common_dir),
            git_dir=str(repo_info.git_dir),
            branch=repo_info.branch,
            initial_head=repo_info.head,
            worktree_key=worktree_key(repo_root),
        ),
        plan_prompt=PlanPromptBinding(
            plan_repository_path=relative_repo_path(repo_info.root, plan_path),
            prompt_source_repository_path=relative_repo_path(repo_info.root, prompt_source_path),
            plan_artifact_path=PLAN_ARTIFACT,
            plan_sha256=artifact_hashes[PLAN_ARTIFACT],
            prompt_artifact_path=PROMPT_ARTIFACT,
            prompt_sha256=artifact_hashes[PROMPT_ARTIFACT],
        ),
        effective_config=EffectiveConfigBinding(
            effective_config_artifact_path=EFFECTIVE_CONFIG_ARTIFACT,
            effective_config_sha256=artifact_hashes[EFFECTIVE_CONFIG_ARTIFACT],
            source_config_artifact_path=SOURCE_CONFIG_ARTIFACT,
            source_config_sha256=artifact_hashes[SOURCE_CONFIG_ARTIFACT],
        ),
        codex=FreshCodexReviewerBinding(
            review_model=review_model,
            review_reasoning_effort=review_reasoning_effort,
            review_model_source="explicit",
            review_reasoning_source="explicit",
            command=effective.codex.command,
            review_skill=effective.codex.review_skill,
            sandbox=effective.codex.sandbox,
            binding_artifact_path=FRESH_REVIEWER_INPUT_ARTIFACT,
            binding_sha256=artifact_hashes[FRESH_REVIEWER_INPUT_ARTIFACT],
        ),
        cursor=CursorBinding(
            command=effective.cursor.command,
            model=effective.cursor.model,
            output_format=effective.cursor.output_format,
            force=effective.cursor.force,
            trust_workspace=effective.cursor.trust_workspace,
            sandbox=effective.cursor.sandbox,
        ),
        workflow=WorkflowLimits(
            max_review_iterations=effective.workflow.max_review_iterations,
            stage_mode=effective.workflow.stage_mode,
            cursor_timeout_minutes=effective.workflow.cursor_timeout_minutes,
            codex_timeout_minutes=effective.workflow.codex_timeout_minutes,
            require_clean_worktree=effective.workflow.require_clean_worktree,
        ),
        controller=ControllerBinding(controller_session_id=controller_session_id),
        baseline_status_artifact_path=BASELINE_STATUS_ARTIFACT,
        baseline_status_sha256=artifact_hashes[BASELINE_STATUS_ARTIFACT],
    )


def _build_context(
    *,
    repo_info: GitRepositoryInfo,
    plan_path: Path,
    prompt_source_path: Path,
    effective: ProjectConfig,
    session_id: str,
    controller_session_id: str,
    review_runtime: EffectiveReviewRuntime,
    artifact_hashes: dict[str, str],
) -> SubmittedRunContext:
    repo_root = str(repo_info.root)
    return SubmittedRunContext(
        project_name=effective.project.name,
        repository=RepositoryBinding(
            root=repo_root,
            git_common_dir=str(repo_info.git_common_dir),
            git_dir=str(repo_info.git_dir),
            branch=repo_info.branch,
            initial_head=repo_info.head,
            worktree_key=worktree_key(repo_root),
        ),
        plan_prompt=PlanPromptBinding(
            plan_repository_path=relative_repo_path(repo_info.root, plan_path),
            prompt_source_repository_path=relative_repo_path(repo_info.root, prompt_source_path),
            plan_artifact_path=PLAN_ARTIFACT,
            plan_sha256=artifact_hashes[PLAN_ARTIFACT],
            prompt_artifact_path=PROMPT_ARTIFACT,
            prompt_sha256=artifact_hashes[PROMPT_ARTIFACT],
        ),
        effective_config=EffectiveConfigBinding(
            effective_config_artifact_path=EFFECTIVE_CONFIG_ARTIFACT,
            effective_config_sha256=artifact_hashes[EFFECTIVE_CONFIG_ARTIFACT],
            source_config_artifact_path=SOURCE_CONFIG_ARTIFACT,
            source_config_sha256=artifact_hashes[SOURCE_CONFIG_ARTIFACT],
        ),
        codex=CodexRuntimeBinding(
            session_id=session_id,
            session_model=review_runtime.session_model,
            session_reasoning_effort=review_runtime.session_reasoning_effort,
            review_model=review_runtime.review_model,
            review_reasoning_effort=review_runtime.review_reasoning_effort,
            review_model_source=review_runtime.review_model_source,  # type: ignore[arg-type]
            review_reasoning_source=review_runtime.review_reasoning_source,  # type: ignore[arg-type]
            model_family_warning=review_runtime.model_family_warning,
            session_origin=review_runtime.session_origin,
            source_event_type=review_runtime.source_event_type,
            source_timestamp=review_runtime.source_timestamp,
            command=effective.codex.command,
            review_skill=effective.codex.review_skill,
            sandbox=effective.codex.sandbox,
            session_runtime_artifact_path=SESSION_RUNTIME_ARTIFACT,
            session_runtime_sha256=artifact_hashes[SESSION_RUNTIME_ARTIFACT],
        ),
        cursor=CursorBinding(
            command=effective.cursor.command,
            model=effective.cursor.model,
            output_format=effective.cursor.output_format,
            force=effective.cursor.force,
            trust_workspace=effective.cursor.trust_workspace,
            sandbox=effective.cursor.sandbox,
        ),
        workflow=WorkflowLimits(
            max_review_iterations=effective.workflow.max_review_iterations,
            stage_mode=effective.workflow.stage_mode,
            cursor_timeout_minutes=effective.workflow.cursor_timeout_minutes,
            codex_timeout_minutes=effective.workflow.codex_timeout_minutes,
            require_clean_worktree=effective.workflow.require_clean_worktree,
        ),
        controller=ControllerBinding(controller_session_id=controller_session_id),
        baseline_status_artifact_path=BASELINE_STATUS_ARTIFACT,
        baseline_status_sha256=artifact_hashes[BASELINE_STATUS_ARTIFACT],
    )


def _result_from_state(state: SubmittedState, *, reused_existing: bool) -> SubmitResult:
    return SubmitResult(
        run_id=state.run_id,
        project_name=state.context.project_name,
        state_kind=state.kind,
        reused_existing=reused_existing,
        safe_next_action=queued_safe_next_action(state.run_id),
    )


class SubmissionService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        repository_discoverer: RepositoryDiscoverer = discover_repository_binding,
        session_runtime_reader: SessionRuntimeReader = read_codex_session_runtime,
        now_factory: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[str, datetime], str] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.repository_discoverer = repository_discoverer
        self.session_runtime_reader = session_runtime_reader
        self._now_factory = now_factory or (lambda: utc_now())
        self._run_id_factory = run_id_factory
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")

    def submit(self, options: SubmitOptions) -> SubmitResult:
        prompt_text = _read_stdin_prompt()
        if options.controller_session_id is None:
            raise ValidationError(
                "controller session id is required for scheduler A/B submit "
                "(--controller-session-id)"
            )
        repo_candidate = options.repo_path or Path.cwd()
        repo_info = self.repository_discoverer(repo_candidate)
        effective, source_repo_config, _repo_config_path = resolve_effective_config(
            repo_root=repo_info.root,
            config_path=options.config_path,
            overrides=_build_overrides(options),
        )
        plan_path, prompt_source_path = _resolve_inputs(options, repo_info)
        controller_session_id = require_codex_session_id(options.controller_session_id)
        if options.codex_session_id is not None:
            raise ValidationError(
                "scheduler submit must not pass --codex-session-id; "
                "reviewer B is created at the first review boundary with frozen "
                "--codex-review-model and --codex-review-reasoning-effort"
            )
        review_model = require_frozen_review_model(options.codex_review_model)
        review_reasoning = require_frozen_review_reasoning_effort(
            options.codex_review_reasoning_effort
        )

        plan_bytes = plan_path.read_bytes()
        effective_yaml = yaml.safe_dump(
            effective.model_dump(by_alias=True),
            sort_keys=False,
            allow_unicode=True,
        ).encode("utf-8")
        source_yaml = yaml.safe_dump(
            source_repo_config.model_dump(by_alias=True),
            sort_keys=False,
            allow_unicode=True,
        ).encode("utf-8")
        baseline_text = repo_info.status_porcelain + "\n"
        fresh_binding_bytes = _fresh_input_artifact_bytes(
            review_model=review_model,
            review_reasoning_effort=review_reasoning,
        )
        artifact_hashes = {
            PLAN_ARTIFACT: sha256_bytes(plan_bytes),
            PROMPT_ARTIFACT: sha256_text(prompt_text),
            EFFECTIVE_CONFIG_ARTIFACT: sha256_bytes(effective_yaml),
            SOURCE_CONFIG_ARTIFACT: sha256_bytes(source_yaml),
            BASELINE_STATUS_ARTIFACT: sha256_text(baseline_text),
            FRESH_REVIEWER_INPUT_ARTIFACT: sha256_bytes(fresh_binding_bytes),
        }
        context = _build_fresh_context(
            repo_info=repo_info,
            plan_path=plan_path,
            prompt_source_path=prompt_source_path,
            effective=effective,
            controller_session_id=controller_session_id,
            review_model=review_model,
            review_reasoning_effort=review_reasoning,
            artifact_hashes=artifact_hashes,
        )
        idempotency_key = canonical_json_sha256({"identity": submission_identity_payload(context)})
        wt_key = context.repository.worktree_key

        with self.store.begin_immediate() as conn:
            existing = self.store.get_run_by_idempotency_key(conn, idempotency_key)
            if existing is not None:
                state, _, _ = self.store.load_validated_snapshot(conn, str(existing["run_id"]))
                return _result_from_state(state, reused_existing=True)
            conflict = self.store.get_active_reservation(conn, wt_key)
            if conflict is not None:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "repository worktree already has an active scheduler reservation "
                    f"(run {conflict['run_id']})",
                )

        now = self._now_factory()
        run_id = (
            self._run_id_factory(effective.project.name, now)
            if self._run_id_factory is not None
            else generate_run_id(effective.project.name, now=now)
        )

        self.artifacts.write_bytes(run_id, PLAN_ARTIFACT, plan_bytes, max_bytes=MAX_PLAN_BYTES)
        self.artifacts.write_text(run_id, PROMPT_ARTIFACT, prompt_text, max_bytes=MAX_PROMPT_BYTES)
        self.artifacts.write_bytes(
            run_id,
            EFFECTIVE_CONFIG_ARTIFACT,
            effective_yaml,
            max_bytes=MAX_PLAN_BYTES,
        )
        self.artifacts.write_bytes(
            run_id,
            SOURCE_CONFIG_ARTIFACT,
            source_yaml,
            max_bytes=MAX_PLAN_BYTES,
        )
        self.artifacts.write_text(
            run_id,
            BASELINE_STATUS_ARTIFACT,
            baseline_text,
            max_bytes=MAX_BASELINE_STATUS_BYTES,
        )
        self.artifacts.write_bytes(
            run_id,
            FRESH_REVIEWER_INPUT_ARTIFACT,
            fresh_binding_bytes,
            max_bytes=MAX_SESSION_RUNTIME_BYTES,
        )

        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        state = SubmittedState(
            run_id=run_id,
            version=1,
            submitted_at=now_text,
            updated_at=now_text,
            idempotency_key=idempotency_key,
            context=context,
        )
        event = RunSubmittedEvent(
            run_id=run_id,
            idempotency_key=idempotency_key,
            worktree_key=wt_key,
            reused_existing=False,
        )
        event_id = self._event_id_factory()

        with self.store.begin_immediate() as conn:
            existing = self.store.get_run_by_idempotency_key(conn, idempotency_key)
            if existing is not None:
                loaded, _, _ = self.store.load_validated_snapshot(conn, str(existing["run_id"]))
                return _result_from_state(loaded, reused_existing=True)
            conflict = self.store.get_active_reservation(conn, wt_key)
            if conflict is not None:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "repository worktree already has an active scheduler reservation "
                    f"(run {conflict['run_id']})",
                )
            self.store.insert_submitted_run(
                conn,
                run_id=run_id,
                state=state,
                event_id=event_id,
                event=event,
                now=now,
            )

        return _result_from_state(state, reused_existing=False)


def default_submission_service(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> SubmissionService:
    store = SqliteSchedulerStore(db_path or default_engine_db_path())
    artifacts = ProtectedArtifactStore(artifact_root or default_artifact_root())
    return SubmissionService(store, artifacts)


def submit_run(options: SubmitOptions) -> SubmitResult:
    service = default_submission_service(
        db_path=options.db_path,
        artifact_root=options.artifact_root,
    )
    return service.submit(options)

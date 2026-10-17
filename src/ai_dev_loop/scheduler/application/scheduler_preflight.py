"""Bounded scheduler preflight and probe adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import discover_repository
from ai_dev_loop.runners.probes import (
    CompatibilityClassification,
    ModelCompatibilityResult,
    ProbeResult,
    executable_probe,
    probe_codex_auth,
    probe_codex_model_compatibility,
    probe_version,
    require_probe_success,
    run_start_probes,
    run_tool_compatibility_probes,
)
from ai_dev_loop.scheduler.application.cursor_evidence import (
    frozen_repository_identity,
    validate_frozen_repository_identity,
)
from ai_dev_loop.scheduler.application.scheduler_checkpoint import checkpoint_from_state
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    CursorSdkBinding,
    RepositoryBinding,
    RepositoryTargetBinding,
    SubmittedRunContext,
    resolved_implementer_provider,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.state import sha256_file


@dataclass(frozen=True)
class SchedulerPreflightResult:
    ok: bool
    failure_kind: str | None = None
    failure_summary: str | None = None


class SchedulerPreflightPort(Protocol):
    def run(
        self,
        *,
        run_id: str,
        context: SubmittedRunContext,
        artifacts: ProtectedArtifactStore,
        state: object | None = None,
    ) -> SchedulerPreflightResult: ...


def _validate_plan_prompt_artifacts(
    context: SubmittedRunContext,
    artifacts: ProtectedArtifactStore,
    run_id: str,
) -> None:
    plan_prompt = context.plan_prompt
    plan_bytes = artifacts.read_verified_bytes(
        run_id,
        plan_prompt.plan_artifact_path,
        expected_sha256=plan_prompt.plan_sha256,
    )
    if plan_bytes is None:
        raise ValidationError("plan artifact hash mismatch")
    prompt_bytes = artifacts.read_verified_bytes(
        run_id,
        plan_prompt.prompt_artifact_path,
        expected_sha256=plan_prompt.prompt_sha256,
    )
    if not prompt_bytes:
        raise ValidationError("prompt artifact hash mismatch")
    repo_root = Path(context.repository.root)
    repo_plan = repo_root / plan_prompt.plan_repository_path
    if not repo_plan.is_file():
        raise ValidationError(f"repository plan file missing: {plan_prompt.plan_repository_path}")
    if sha256_file(repo_plan) != plan_prompt.plan_sha256:
        raise ValidationError("repository plan file hash does not match frozen binding")
    source_path = repo_root / plan_prompt.prompt_source_repository_path
    if source_path.is_file() and sha256_file(source_path) != plan_prompt.prompt_sha256:
        raise ValidationError("prompt source file hash does not match frozen binding")


def _validate_repository_identity(
    context: SubmittedRunContext,
    repo_root: Path,
    *,
    run_id: str,
    artifacts: ProtectedArtifactStore,
    checkpoint: AdmittedRunCheckpoint | None,
) -> None:
    repository = context.repository
    if isinstance(repository, RepositoryBinding):
        discovered = discover_repository(repo_root)
        if discovered.branch != repository.branch:
            raise ValidationError("repository branch changed since submission")
        if discovered.head != repository.initial_head:
            raise ValidationError("repository HEAD changed since submission")
        if str(discovered.git_common_dir) != repository.git_common_dir:
            raise ValidationError("repository git common dir changed since submission")
        if str(discovered.git_dir) != repository.git_dir:
            raise ValidationError("repository git dir changed since submission")
    elif isinstance(repository, RepositoryTargetBinding):
        if str(repo_root.resolve()) != str(Path(repository.root).resolve()):
            raise ValidationError("repository root changed since submission")
        if checkpoint is None:
            raise ValidationError("admission checkpoint required for repository target binding")
        identity = frozen_repository_identity(
            context,
            run_id=run_id,
            artifacts=artifacts,
            checkpoint=checkpoint,
        )
        validate_frozen_repository_identity(
            repo_root,
            identity,
            context="scheduler preflight",
        )


def _validate_baseline_if_present(
    context: SubmittedRunContext,
    artifacts: ProtectedArtifactStore,
    run_id: str,
    repo_root: Path,
) -> None:
    if context.baseline_status_artifact_path is None or context.baseline_status_sha256 is None:
        return
    baseline_bytes = artifacts.read_verified_bytes(
        run_id,
        context.baseline_status_artifact_path,
        expected_sha256=context.baseline_status_sha256,
    )
    if baseline_bytes is None:
        raise ValidationError("baseline status artifact hash mismatch")
    baseline_status = baseline_bytes.decode("utf-8").rstrip("\n")
    current_status = discover_repository(repo_root).status_porcelain
    if current_status != baseline_status:
        raise ValidationError("worktree status changed since prepare")


def _codex_implementer_start_probes(
    *,
    implementer_command: str,
    reviewer_command: str,
) -> list[ProbeResult]:
    commands = [("Git", "git"), ("Codex", implementer_command)]
    if reviewer_command != implementer_command:
        commands.append(("Codex", reviewer_command))
    failures: list[ProbeResult] = []
    for label, command in commands:
        failure = executable_probe(command, label=label)
        if failure is not None:
            failures.append(failure)
    if failures:
        return failures
    probes = [probe_codex_auth(implementer_command)]
    if reviewer_command != implementer_command:
        probes.append(probe_codex_auth(reviewer_command))
    return probes


def _codex_implementer_compatibility(
    *,
    implementer_command: str,
    implementer_model: str,
    implementer_effort: str,
    reviewer_command: str,
    reviewer_model: str,
) -> list[ModelCompatibilityResult]:
    implementer_version = probe_version(implementer_command, tool="codex")
    results = [
        probe_codex_model_compatibility(
            implementer_command,
            required_model=implementer_model,
            installed_version=implementer_version.version,
            required_reasoning_effort=implementer_effort,
        )
    ]
    if reviewer_command == implementer_command and reviewer_model == implementer_model:
        reviewer_version = implementer_version.version
    else:
        reviewer_version = probe_version(reviewer_command, tool="codex").version
    results.append(
        probe_codex_model_compatibility(
            reviewer_command,
            required_model=reviewer_model,
            installed_version=reviewer_version,
        )
    )
    return results


def _compatibility_failure(
    compat: list[ModelCompatibilityResult],
    *,
    reject_probe_failure: bool,
) -> SchedulerPreflightResult | None:
    incompatible = [
        item
        for item in compat
        if item.classification == CompatibilityClassification.INCOMPATIBLE_MODEL
    ]
    if incompatible:
        detail = incompatible[0].detail or "tool model compatibility check failed"
        return SchedulerPreflightResult(
            ok=False,
            failure_kind="tool_incompatible",
            failure_summary=detail,
        )
    unknown = [
        item for item in compat if item.classification == CompatibilityClassification.UNKNOWN
    ]
    if unknown:
        detail = unknown[0].detail or "tool model compatibility is unknown"
        return SchedulerPreflightResult(
            ok=False,
            failure_kind="tool_compatibility_unknown",
            failure_summary=detail,
        )
    failed = [
        item for item in compat if item.classification == CompatibilityClassification.PROBE_FAILED
    ]
    if reject_probe_failure and failed:
        detail = failed[0].detail or "tool model compatibility probe failed"
        return SchedulerPreflightResult(
            ok=False,
            failure_kind="preflight_validation_failed",
            failure_summary=detail,
        )
    return None


def run_scheduler_preflight(
    *,
    run_id: str,
    context: SubmittedRunContext,
    artifacts: ProtectedArtifactStore,
    checkpoint: AdmittedRunCheckpoint | None = None,
    state: object | None = None,
) -> SchedulerPreflightResult:
    repo_root = Path(context.repository.root)
    if checkpoint is None and state is not None:
        checkpoint = checkpoint_from_state(state)
    try:
        if not repo_root.is_dir():
            raise ValidationError("repository root does not exist")
        _validate_repository_identity(
            context,
            repo_root,
            run_id=run_id,
            artifacts=artifacts,
            checkpoint=checkpoint,
        )
        _validate_plan_prompt_artifacts(context, artifacts, run_id)
        _validate_baseline_if_present(context, artifacts, run_id, repo_root)
        provider = resolved_implementer_provider(context)
        if provider == "codex":
            implementer = context.implementer
            if implementer is None or implementer.reasoning_effort is None:
                raise ValidationError("codex implementer binding is incomplete")
            assert implementer.command is not None
            require_probe_success(
                _codex_implementer_start_probes(
                    implementer_command=implementer.command,
                    reviewer_command=context.codex.command,
                )
            )
            compat = _codex_implementer_compatibility(
                implementer_command=implementer.command,
                implementer_model=implementer.model,
                implementer_effort=implementer.reasoning_effort,
                reviewer_command=context.codex.command,
                reviewer_model=context.codex.review_model,
            )
        elif isinstance(context.cursor, CursorSdkBinding):
            # SDK package/path/auth/catalog checks execute inside the owned attempt
            # unit. Preflight stays inference-free while still checking Git and B.
            require_probe_success(
                [
                    item
                    for item in (
                        executable_probe("git", label="Git"),
                        executable_probe(context.codex.command, label="Codex"),
                        probe_codex_auth(context.codex.command),
                    )
                    if item is not None
                ]
            )
            reviewer_version = probe_version(context.codex.command, tool="codex")
            compat = [
                probe_codex_model_compatibility(
                    context.codex.command,
                    required_model=context.codex.review_model,
                    installed_version=reviewer_version.version,
                )
            ]
        else:
            require_probe_success(
                run_start_probes(
                    cursor_command=context.cursor.command,
                    cursor_model=context.cursor.model,
                    codex_command=context.codex.command,
                )
            )
            compat, _versions = run_tool_compatibility_probes(
                cursor_command=context.cursor.command,
                cursor_model=context.cursor.model,
                codex_command=context.codex.command,
                codex_model=context.codex.review_model,
            )
        failure = _compatibility_failure(compat, reject_probe_failure=provider == "codex")
        if failure is not None:
            return failure
    except ValidationError as exc:
        return SchedulerPreflightResult(
            ok=False,
            failure_kind="preflight_validation_failed",
            failure_summary=str(exc),
        )
    except ProtectedArtifactError as exc:
        return SchedulerPreflightResult(
            ok=False,
            failure_kind="preflight_artifact_failed",
            failure_summary=str(exc)[:240],
        )
    except OSError:
        return SchedulerPreflightResult(
            ok=False,
            failure_kind="preflight_io_failed",
            failure_summary="preflight artifact access failed",
        )
    return SchedulerPreflightResult(ok=True)


class DefaultSchedulerPreflightPort:
    def run(
        self,
        *,
        run_id: str,
        context: SubmittedRunContext,
        artifacts: ProtectedArtifactStore,
        state: object | None = None,
    ) -> SchedulerPreflightResult:
        return run_scheduler_preflight(
            run_id=run_id,
            context=context,
            artifacts=artifacts,
            state=state,
        )

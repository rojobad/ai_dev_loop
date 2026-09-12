"""Scheduler sequence preparation service."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import yaml
from pydantic import ValidationError as PydanticValidationError

from ai_dev_loop.config import ConfigOverrides, resolve_effective_config
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.fresh_codex_reviewer import (
    build_fresh_reviewer_input_artifact,
    require_frozen_review_model,
    require_frozen_review_reasoning_effort,
)
from ai_dev_loop.integrations.codex.session_runtime import require_codex_session_id
from ai_dev_loop.runners.git import relative_repo_path, resolve_repo_relative_path
from ai_dev_loop.scheduler.application.contracts import (
    SequencePrepareResult,
    prepared_sequence_safe_next_action,
)
from ai_dev_loop.scheduler.application.submission import (
    _freeze_execution_commands,
    require_resubmission_id,
)
from ai_dev_loop.scheduler.domain.common import canonical_json_sha256, worktree_key
from ai_dev_loop.scheduler.domain.sequence import (
    FROZEN_SEQUENCE_ENTRY_SCHEMA_VERSION,
    MAX_MANIFEST_BYTES,
    PREPARED_SEQUENCE_SCHEMA_VERSION,
    PREPARED_SEQUENCE_STATE_KIND,
    FrozenSequenceEntry,
    PreparedSequenceDefinition,
    PreparedSequenceState,
    SequenceManifest,
    SequenceManifestPhase,
    sequence_identity_payload,
)
from ai_dev_loop.scheduler.domain.state import (
    ControllerBinding,
    CursorBinding,
    EffectiveConfigBinding,
    FreshCodexReviewerBinding,
    PlanPromptBinding,
    RepositoryTargetBinding,
    WorkflowLimits,
)
from ai_dev_loop.scheduler.infrastructure.paths import (
    default_artifact_root,
    default_engine_db_path,
    reject_repository_overlap,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    MAX_CONFIG_BYTES,
    MAX_PLAN_BYTES,
    MAX_PROMPT_BYTES,
    MAX_SESSION_RUNTIME_BYTES,
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.repository_target import (
    RepositoryTarget,
    resolve_repository_target,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import (
    sha256_bytes,
    sha256_text,
    utc_now,
)

IDENTITY_PLACEHOLDER = "identity-placeholder"

MANIFEST_ORIGINAL_ARTIFACT = "manifest/original.yaml"
MANIFEST_RESOLVED_ARTIFACT = "manifest/resolved.yaml"
FRESH_REVIEWER_INPUT_ARTIFACT = "codex/fresh-reviewer-input.json"
EFFECTIVE_CONFIG_ARTIFACT = "effective-config.yaml"
SOURCE_CONFIG_ARTIFACT = "source-config.yaml"
PLAN_ARTIFACT_NAME = "plan/plan.md"
PROMPT_ARTIFACT_NAME = "prompts/cursor-initial.txt"

RepositoryDiscoverer = Callable[[Path], RepositoryTarget]
PrepareStepHook = Callable[[str], None]


def _format_manifest_validation_error(exc: PydanticValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        err_type = str(error.get("type", "value_error"))
        if location:
            messages.append(f"{location}: {err_type}")
        else:
            messages.append(err_type)
    if not messages:
        return "sequence manifest failed validation"
    return "sequence manifest failed validation: " + "; ".join(messages)


def _derive_sequence_id(project_name: str, idempotency_key: str) -> str:
    token = idempotency_key[:12]
    return f"{project_name}-seq-{token}"


def _derive_planned_run_id(project_name: str, idempotency_key: str, ordinal: int) -> str:
    token = sha256_text(f"{idempotency_key}:{ordinal:02d}")[:12]
    return f"{project_name}-{token}"


def _entry_prefix(ordinal: int) -> str:
    return f"entries/{ordinal:02d}"


def _entry_plan_artifact(ordinal: int) -> str:
    return f"{_entry_prefix(ordinal)}/{PLAN_ARTIFACT_NAME}"


def _entry_prompt_artifact(ordinal: int) -> str:
    return f"{_entry_prefix(ordinal)}/{PROMPT_ARTIFACT_NAME}"


def _entry_effective_config_artifact(ordinal: int) -> str:
    return f"{_entry_prefix(ordinal)}/{EFFECTIVE_CONFIG_ARTIFACT}"


def _entry_source_config_artifact(ordinal: int) -> str:
    return f"{_entry_prefix(ordinal)}/{SOURCE_CONFIG_ARTIFACT}"


def _entry_fresh_reviewer_artifact(ordinal: int) -> str:
    return f"{_entry_prefix(ordinal)}/{FRESH_REVIEWER_INPUT_ARTIFACT}"


def _sequence_idempotency_key(
    definition: PreparedSequenceDefinition,
    *,
    resubmission_id: str | None = None,
) -> str:
    payload: dict[str, object] = {"identity": sequence_identity_payload(definition)}
    if resubmission_id is not None:
        payload["resubmission_sha256"] = sha256_text(resubmission_id)
    return canonical_json_sha256(payload)


def _load_manifest_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise ValidationError(f"sequence manifest not found: {path}")
    if path.is_symlink():
        raise ValidationError("sequence manifest path must not be a symlink")
    data = path.read_bytes()
    if not data.strip():
        raise ValidationError("sequence manifest is empty")
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValidationError("sequence manifest exceeds size limit")
    return data


def _parse_manifest(data: bytes) -> SequenceManifest:
    try:
        raw = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise ValidationError("sequence manifest is not valid YAML") from exc
    if not isinstance(raw, dict):
        raise ValidationError("sequence manifest must be a YAML mapping")
    try:
        return SequenceManifest.model_validate(raw)
    except PydanticValidationError as exc:
        raise ValidationError(_format_manifest_validation_error(exc)) from exc


def _phase_overrides(
    global_options: SequencePrepareOptions,
    phase: SequenceManifestPhase,
) -> ConfigOverrides:
    cursor = phase.cursor
    codex = phase.codex
    workflow = phase.workflow

    def pick_str(global_value: str | None, phase_value: str | None) -> str | None:
        return phase_value if phase_value is not None else global_value

    def pick_int(global_value: int | None, phase_value: int | None) -> int | None:
        return phase_value if phase_value is not None else global_value

    return ConfigOverrides(
        project_name=global_options.project_name,
        cursor_command=pick_str(
            global_options.cursor_command, None if cursor is None else cursor.command
        ),
        cursor_model=pick_str(
            global_options.cursor_model, None if cursor is None else cursor.model
        ),
        cursor_output_format=pick_str(
            global_options.cursor_output_format,
            None if cursor is None else cursor.output_format,
        ),
        codex_command=pick_str(global_options.codex_command, codex.command),
        codex_review_model=codex.review_model,
        codex_review_reasoning_effort=codex.review_reasoning_effort,
        review_skill=pick_str(global_options.review_skill, codex.review_skill),
        max_review_iterations=pick_int(
            global_options.max_review_iterations,
            None if workflow is None else workflow.max_review_iterations,
        ),
        cursor_timeout_minutes=pick_int(
            global_options.cursor_timeout_minutes,
            None if workflow is None else workflow.cursor_timeout_minutes,
        ),
        codex_timeout_minutes=pick_int(
            global_options.codex_timeout_minutes,
            None if workflow is None else workflow.codex_timeout_minutes,
        ),
    )


def _resolve_phase_inputs(
    repo_target: RepositoryTarget,
    phase: SequenceManifestPhase,
) -> tuple[Path, Path]:
    plan_path = resolve_repo_relative_path(repo_target.root, Path(phase.plan_path))
    prompt_source_path = resolve_repo_relative_path(
        repo_target.root,
        Path(phase.prompt_source_path),
    )
    if not plan_path.is_file():
        raise ValidationError(f"plan file not found for phase {phase.name!r}: {plan_path.name}")
    if plan_path.is_symlink():
        raise ValidationError(f"plan path for phase {phase.name!r} must not be a symlink")
    if not prompt_source_path.is_file():
        raise ValidationError(
            f"prompt source file not found for phase {phase.name!r}: {prompt_source_path.name}"
        )
    if prompt_source_path.is_symlink():
        raise ValidationError(f"prompt source path for phase {phase.name!r} must not be a symlink")
    plan_bytes = plan_path.read_bytes()
    prompt_bytes = prompt_source_path.read_bytes()
    if not plan_bytes.strip():
        raise ValidationError(f"plan file for phase {phase.name!r} is empty")
    if not prompt_bytes.strip():
        raise ValidationError(f"prompt source file for phase {phase.name!r} is empty")
    if len(plan_bytes) > MAX_PLAN_BYTES:
        raise ValidationError(f"plan file for phase {phase.name!r} exceeds size limit")
    if len(prompt_bytes) > MAX_PROMPT_BYTES:
        raise ValidationError(f"prompt source file for phase {phase.name!r} exceeds size limit")
    return plan_path, prompt_source_path


def _fresh_input_artifact_bytes(*, review_model: str, review_reasoning_effort: str) -> bytes:
    payload = build_fresh_reviewer_input_artifact(
        review_model=review_model,
        review_reasoning_effort=review_reasoning_effort,
    )
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def _build_resolved_manifest_payload(manifest: SequenceManifest) -> dict[str, object]:
    return manifest.model_dump(mode="json")


@dataclass(frozen=True)
class SequencePrepareOptions:
    manifest_path: Path
    config_path: Path | None = None
    project_name: str | None = None
    repo_path: Path | None = None
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
    resubmission_id: str | None = None


class SequencePrepareService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        repository_discoverer: RepositoryDiscoverer = resolve_repository_target,
        now_factory: Callable[[], datetime] | None = None,
        sequence_id_factory: Callable[[str, datetime], str] | None = None,
        run_id_factory: Callable[[str, datetime], str] | None = None,
        prepare_step_hook: PrepareStepHook | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.repository_discoverer = repository_discoverer
        self._now_factory = now_factory or (lambda: utc_now())
        self._sequence_id_factory = sequence_id_factory
        self._run_id_factory = run_id_factory
        self._prepare_step_hook = prepare_step_hook

    def _prepare_step(self, step: str) -> None:
        if self._prepare_step_hook is not None:
            self._prepare_step_hook(step)

    def _verify_reused_sequence(self, state: PreparedSequenceState) -> None:
        try:
            self.artifacts.verify_prepared_sequence_artifacts(
                state.sequence_id,
                state.definition,
            )
        except ProtectedArtifactError as exc:
            raise ValidationError(
                "prepared sequence artifacts failed integrity verification"
            ) from exc

    def _reuse_existing_result(self, state: PreparedSequenceState) -> SequencePrepareResult:
        self._verify_reused_sequence(state)
        return SequencePrepareResult(
            sequence_id=state.sequence_id,
            name=state.definition.name,
            state_kind=PREPARED_SEQUENCE_STATE_KIND,
            entry_count=len(state.definition.entries),
            reused_existing=True,
            safe_next_action=prepared_sequence_safe_next_action(),
        )

    def prepare(self, options: SequencePrepareOptions) -> SequencePrepareResult:
        manifest_bytes = _load_manifest_bytes(options.manifest_path)
        manifest = _parse_manifest(manifest_bytes)
        repo_candidate = options.repo_path or Path.cwd()
        repo_target = self.repository_discoverer(repo_candidate)
        controller_session_id = (
            require_codex_session_id(options.controller_session_id)
            if options.controller_session_id is not None
            else None
        )
        resubmission_id = (
            require_resubmission_id(options.resubmission_id)
            if options.resubmission_id is not None
            else None
        )

        resolved_manifest_payload = _build_resolved_manifest_payload(manifest)
        resolved_manifest_bytes = yaml.safe_dump(
            resolved_manifest_payload, sort_keys=False, allow_unicode=True
        ).encode("utf-8")
        if len(resolved_manifest_bytes) > MAX_MANIFEST_BYTES:
            raise ValidationError("resolved sequence manifest exceeds size limit")

        now = self._now_factory()
        frozen_entries: list[FrozenSequenceEntry] = []
        phase_artifact_bytes: list[tuple[bytes, bytes, bytes, bytes, bytes]] = []
        project_name: str | None = None
        for ordinal, phase in enumerate(manifest.phases, start=1):
            effective, source_repo_config, _repo_config_path = resolve_effective_config(
                repo_root=repo_target.root,
                config_path=options.config_path,
                overrides=_phase_overrides(options, phase),
            )
            effective = _freeze_execution_commands(effective)
            if project_name is None:
                project_name = effective.project.name
            elif effective.project.name != project_name:
                raise ValidationError(
                    "all sequence phases must resolve to the same project.name after overrides"
                )

            plan_path, prompt_source_path = _resolve_phase_inputs(repo_target, phase)
            plan_bytes = plan_path.read_bytes()
            prompt_bytes = prompt_source_path.read_bytes()
            review_model = require_frozen_review_model(phase.codex.review_model)
            review_reasoning = require_frozen_review_reasoning_effort(
                phase.codex.review_reasoning_effort
            )

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
            fresh_binding_bytes = _fresh_input_artifact_bytes(
                review_model=review_model,
                review_reasoning_effort=review_reasoning,
            )
            phase_artifact_bytes.append(
                (plan_bytes, prompt_bytes, effective_yaml, source_yaml, fresh_binding_bytes)
            )
            artifact_hashes = {
                _entry_plan_artifact(ordinal): sha256_bytes(plan_bytes),
                _entry_prompt_artifact(ordinal): sha256_bytes(prompt_bytes),
                _entry_effective_config_artifact(ordinal): sha256_bytes(effective_yaml),
                _entry_source_config_artifact(ordinal): sha256_bytes(source_yaml),
                _entry_fresh_reviewer_artifact(ordinal): sha256_bytes(fresh_binding_bytes),
            }
            commit_message = phase.commit_message.strip() if phase.commit_message else None
            if ordinal == len(manifest.phases):
                commit_message = None
            frozen_entries.append(
                FrozenSequenceEntry(
                    schema_version=cast(Literal[1], FROZEN_SEQUENCE_ENTRY_SCHEMA_VERSION),
                    ordinal=ordinal,
                    phase_name=phase.name,
                    planned_run_id=IDENTITY_PLACEHOLDER,
                    commit_message=commit_message,
                    plan_prompt=PlanPromptBinding(
                        plan_repository_path=relative_repo_path(repo_target.root, plan_path),
                        prompt_source_repository_path=relative_repo_path(
                            repo_target.root,
                            prompt_source_path,
                        ),
                        plan_artifact_path=_entry_plan_artifact(ordinal),
                        plan_sha256=artifact_hashes[_entry_plan_artifact(ordinal)],
                        prompt_artifact_path=_entry_prompt_artifact(ordinal),
                        prompt_sha256=artifact_hashes[_entry_prompt_artifact(ordinal)],
                    ),
                    effective_config=EffectiveConfigBinding(
                        effective_config_artifact_path=_entry_effective_config_artifact(ordinal),
                        effective_config_sha256=artifact_hashes[
                            _entry_effective_config_artifact(ordinal)
                        ],
                        source_config_artifact_path=_entry_source_config_artifact(ordinal),
                        source_config_sha256=artifact_hashes[
                            _entry_source_config_artifact(ordinal)
                        ],
                    ),
                    codex=FreshCodexReviewerBinding(
                        review_model=review_model,
                        review_reasoning_effort=review_reasoning,
                        review_model_source="explicit",
                        review_reasoning_source="explicit",
                        command=effective.codex.command,
                        review_skill=effective.codex.review_skill,
                        sandbox=effective.codex.sandbox,
                        binding_artifact_path=_entry_fresh_reviewer_artifact(ordinal),
                        binding_sha256=artifact_hashes[_entry_fresh_reviewer_artifact(ordinal)],
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
                )
            )

        assert project_name is not None
        identity_definition = PreparedSequenceDefinition(
            schema_version=cast(Literal[1], PREPARED_SEQUENCE_SCHEMA_VERSION),
            sequence_id=IDENTITY_PLACEHOLDER,
            name=manifest.name,
            project_name=project_name,
            repository=RepositoryTargetBinding(
                root=str(repo_target.root),
                worktree_key=worktree_key(str(repo_target.root)),
            ),
            manifest_original_artifact_path=MANIFEST_ORIGINAL_ARTIFACT,
            manifest_original_sha256=sha256_bytes(manifest_bytes),
            manifest_resolved_artifact_path=MANIFEST_RESOLVED_ARTIFACT,
            manifest_resolved_sha256=sha256_bytes(resolved_manifest_bytes),
            controller=ControllerBinding(controller_session_id=controller_session_id),
            entries=tuple(frozen_entries),
        )

        with self.store.begin_immediate() as conn:
            self.store.require_sequence_schema(conn)
            existing = self.store.find_existing_prepared_sequence(
                conn,
                definition=identity_definition,
                resubmission_id=resubmission_id,
            )
            if existing is not None:
                existing_state = self.store.load_validated_sequence_state(
                    conn,
                    str(existing["sequence_id"]),
                )
                return self._reuse_existing_result(existing_state)

            idempotency_key = _sequence_idempotency_key(
                identity_definition,
                resubmission_id=resubmission_id,
            )
            sequence_id = (
                self._sequence_id_factory(project_name, now)
                if self._sequence_id_factory is not None
                else _derive_sequence_id(project_name, idempotency_key)
            )
            assigned_entries = tuple(
                entry.model_copy(
                    update={
                        "planned_run_id": (
                            self._run_id_factory(project_name, now)
                            if self._run_id_factory is not None
                            else _derive_planned_run_id(
                                project_name,
                                idempotency_key,
                                entry.ordinal,
                            )
                        )
                    }
                )
                for entry in frozen_entries
            )
            definition = identity_definition.model_copy(
                update={"sequence_id": sequence_id, "entries": assigned_entries}
            )
            final_idempotency_key = _sequence_idempotency_key(
                definition,
                resubmission_id=resubmission_id,
            )
            now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            state = PreparedSequenceState(
                schema_version=1,
                sequence_id=sequence_id,
                version=1,
                prepared_at=now_text,
                updated_at=now_text,
                idempotency_key=final_idempotency_key,
                definition=definition,
            )

        self._write_sequence_artifacts(
            sequence_id=sequence_id,
            manifest_bytes=manifest_bytes,
            resolved_manifest_bytes=resolved_manifest_bytes,
            phase_artifact_bytes=phase_artifact_bytes,
            definition=definition,
            repository_root=repo_target.root,
        )
        self._prepare_step("artifacts_written")

        from ai_dev_loop.scheduler.application.contracts import (
            SchedulerEngineError,
            SchedulerEngineErrorKind,
        )

        with self.store.begin_immediate() as conn:
            self.store.require_sequence_schema(conn)
            existing = self.store.find_existing_prepared_sequence(
                conn,
                definition=identity_definition,
                resubmission_id=resubmission_id,
            )
            if existing is not None:
                existing_state = self.store.load_validated_sequence_state(
                    conn,
                    str(existing["sequence_id"]),
                )
                return self._reuse_existing_result(existing_state)
            self._prepare_step("before_db_insert")
            try:
                self.artifacts.verify_prepared_sequence_artifacts(sequence_id, definition)
            except ProtectedArtifactError as exc:
                raise ValidationError(
                    "prepared sequence artifacts failed integrity verification"
                ) from exc
            try:
                self.store.insert_prepared_sequence(conn, state=state, now=now)
            except SchedulerEngineError as exc:
                if exc.kind is not SchedulerEngineErrorKind.CONFLICT:
                    raise
                existing = self.store.find_existing_prepared_sequence(
                    conn,
                    definition=identity_definition,
                    resubmission_id=resubmission_id,
                )
                if existing is None:
                    raise
                existing_state = self.store.load_validated_sequence_state(
                    conn,
                    str(existing["sequence_id"]),
                )
                return self._reuse_existing_result(existing_state)
            self._prepare_step("after_db_insert")

            return SequencePrepareResult(
                sequence_id=sequence_id,
                name=manifest.name,
                state_kind=PREPARED_SEQUENCE_STATE_KIND,
                entry_count=len(assigned_entries),
                reused_existing=False,
                safe_next_action=prepared_sequence_safe_next_action(),
            )

    def _write_sequence_artifacts(
        self,
        *,
        sequence_id: str,
        manifest_bytes: bytes,
        resolved_manifest_bytes: bytes,
        phase_artifact_bytes: list[tuple[bytes, bytes, bytes, bytes, bytes]],
        definition: PreparedSequenceDefinition,
        repository_root: Path,
    ) -> None:
        reject_repository_overlap(self.artifacts.artifact_root, repository_root)
        self.artifacts.write_sequence_bytes_or_verify(
            sequence_id,
            MANIFEST_ORIGINAL_ARTIFACT,
            manifest_bytes,
            max_bytes=MAX_MANIFEST_BYTES,
        )
        self._prepare_step("manifest_original")
        self.artifacts.write_sequence_text_or_verify(
            sequence_id,
            MANIFEST_RESOLVED_ARTIFACT,
            resolved_manifest_bytes.decode("utf-8"),
            max_bytes=MAX_MANIFEST_BYTES,
        )
        for ordinal, artifacts in enumerate(phase_artifact_bytes, start=1):
            plan_bytes, prompt_bytes, effective_yaml, source_yaml, fresh_binding_bytes = artifacts
            self.artifacts.write_sequence_text_or_verify(
                sequence_id,
                _entry_plan_artifact(ordinal),
                plan_bytes.decode("utf-8"),
                max_bytes=MAX_PLAN_BYTES,
            )
            self.artifacts.write_sequence_text_or_verify(
                sequence_id,
                _entry_prompt_artifact(ordinal),
                prompt_bytes.decode("utf-8"),
                max_bytes=MAX_PROMPT_BYTES,
            )
            self.artifacts.write_sequence_text_or_verify(
                sequence_id,
                _entry_effective_config_artifact(ordinal),
                effective_yaml.decode("utf-8"),
                max_bytes=MAX_CONFIG_BYTES,
            )
            self.artifacts.write_sequence_text_or_verify(
                sequence_id,
                _entry_source_config_artifact(ordinal),
                source_yaml.decode("utf-8"),
                max_bytes=MAX_CONFIG_BYTES,
            )
            self.artifacts.write_sequence_text_or_verify(
                sequence_id,
                _entry_fresh_reviewer_artifact(ordinal),
                fresh_binding_bytes.decode("utf-8"),
                max_bytes=MAX_SESSION_RUNTIME_BYTES,
            )


def default_sequence_prepare_service(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> SequencePrepareService:
    store = SqliteSchedulerStore(db_path or default_engine_db_path())
    artifacts = ProtectedArtifactStore(artifact_root or default_artifact_root())
    return SequencePrepareService(store, artifacts)


def prepare_sequence(options: SequencePrepareOptions) -> SequencePrepareResult:
    service = default_sequence_prepare_service(
        db_path=options.db_path,
        artifact_root=options.artifact_root,
    )
    return service.prepare(options)

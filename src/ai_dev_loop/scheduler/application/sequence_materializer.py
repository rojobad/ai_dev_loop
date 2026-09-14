"""Materialize scheduler run artifacts from frozen sequence entries."""

from __future__ import annotations

from ai_dev_loop.scheduler.domain.sequence import (
    FrozenSequenceEntry,
    PreparedSequenceDefinition,
)
from ai_dev_loop.scheduler.domain.state import (
    SUBMITTED_CONTEXT_SCHEMA_VERSION_SEQUENCE,
    ControllerBinding,
    CursorBinding,
    EffectiveConfigBinding,
    FreshCodexReviewerBinding,
    PlanPromptBinding,
    RepositoryTargetBinding,
    SequenceRunBinding,
    SubmittedRunContext,
    WorkflowLimits,
    submission_identity_payload,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    MAX_CONFIG_BYTES,
    MAX_PLAN_BYTES,
    MAX_PROMPT_BYTES,
    MAX_SESSION_RUNTIME_BYTES,
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

FRESH_REVIEWER_INPUT_ARTIFACT = "codex/fresh-reviewer-input.json"
PLAN_ARTIFACT = "plan/plan.md"
PROMPT_ARTIFACT = "prompts/cursor-initial.txt"
EFFECTIVE_CONFIG_ARTIFACT = "effective-config.yaml"
SOURCE_CONFIG_ARTIFACT = "source-config.yaml"

SEQUENCE_MATERIALIZED_RUN_ARTIFACT_PATHS = frozenset(
    {
        PLAN_ARTIFACT,
        PROMPT_ARTIFACT,
        EFFECTIVE_CONFIG_ARTIFACT,
        SOURCE_CONFIG_ARTIFACT,
        FRESH_REVIEWER_INPUT_ARTIFACT,
    }
)


def frozen_entry_hash(entry: FrozenSequenceEntry) -> str:
    _, digest = SqliteSchedulerStore.dump_sequence_entry(entry)
    return digest


def build_sequence_run_context(
    *,
    definition: PreparedSequenceDefinition,
    entry: FrozenSequenceEntry,
    entry_hash: str,
) -> SubmittedRunContext:
    return SubmittedRunContext(
        schema_version=SUBMITTED_CONTEXT_SCHEMA_VERSION_SEQUENCE,
        project_name=definition.project_name,
        repository=RepositoryTargetBinding(
            root=definition.repository.root,
            worktree_key=definition.repository.worktree_key,
        ),
        plan_prompt=PlanPromptBinding(
            plan_repository_path=entry.plan_prompt.plan_repository_path,
            prompt_source_repository_path=entry.plan_prompt.prompt_source_repository_path,
            plan_artifact_path=PLAN_ARTIFACT,
            plan_sha256=entry.plan_prompt.plan_sha256,
            prompt_artifact_path=PROMPT_ARTIFACT,
            prompt_sha256=entry.plan_prompt.prompt_sha256,
        ),
        effective_config=EffectiveConfigBinding(
            effective_config_artifact_path=EFFECTIVE_CONFIG_ARTIFACT,
            effective_config_sha256=entry.effective_config.effective_config_sha256,
            source_config_artifact_path=SOURCE_CONFIG_ARTIFACT,
            source_config_sha256=entry.effective_config.source_config_sha256,
        ),
        codex=FreshCodexReviewerBinding(
            review_model=entry.codex.review_model,
            review_reasoning_effort=entry.codex.review_reasoning_effort,
            review_model_source=entry.codex.review_model_source,
            review_reasoning_source=entry.codex.review_reasoning_source,
            command=entry.codex.command,
            review_skill=entry.codex.review_skill,
            sandbox=entry.codex.sandbox,
            binding_artifact_path=FRESH_REVIEWER_INPUT_ARTIFACT,
            binding_sha256=entry.codex.binding_sha256,
        ),
        cursor=CursorBinding(
            command=entry.cursor.command,
            model=entry.cursor.model,
            output_format=entry.cursor.output_format,
            force=entry.cursor.force,
            trust_workspace=entry.cursor.trust_workspace,
            sandbox=entry.cursor.sandbox,
        ),
        workflow=WorkflowLimits(
            max_review_iterations=entry.workflow.max_review_iterations,
            stage_mode=entry.workflow.stage_mode,
            cursor_timeout_minutes=entry.workflow.cursor_timeout_minutes,
            codex_timeout_minutes=entry.workflow.codex_timeout_minutes,
            require_clean_worktree=entry.workflow.require_clean_worktree,
        ),
        controller=ControllerBinding(
            controller_session_id=definition.controller.controller_session_id,
        ),
        sequence=SequenceRunBinding(
            sequence_id=definition.sequence_id,
            ordinal=entry.ordinal,
            total_phases=len(definition.entries),
            entry_hash=entry_hash,
        ),
    )


def sequence_run_idempotency_key(context: SubmittedRunContext) -> str:
    from ai_dev_loop.scheduler.domain.common import canonical_json_sha256

    return canonical_json_sha256({"identity": submission_identity_payload(context)})


class SequenceRunMaterializer:
    def __init__(self, artifacts: ProtectedArtifactStore) -> None:
        self.artifacts = artifacts

    def materialize_first_entry(
        self,
        *,
        sequence_id: str,
        definition: PreparedSequenceDefinition,
        entry: FrozenSequenceEntry,
    ) -> tuple[SubmittedRunContext, str]:
        if entry.ordinal != 1:
            raise ValueError("Phase 20.2 materializes only ordinal 1")
        if entry.planned_run_id != definition.entries[0].planned_run_id:
            raise ValueError("entry planned_run_id disagrees with sequence definition")
        entry_hash = frozen_entry_hash(entry)
        context = build_sequence_run_context(
            definition=definition,
            entry=entry,
            entry_hash=entry_hash,
        )
        run_id = entry.planned_run_id
        self._validate_orphan_run_artifact_tree(run_id)
        self._copy_entry_artifacts(
            sequence_id=sequence_id,
            run_id=run_id,
            entry=entry,
        )
        return context, entry_hash

    def materialize_next_entry(
        self,
        *,
        sequence_id: str,
        definition: PreparedSequenceDefinition,
        entry: FrozenSequenceEntry,
    ) -> tuple[SubmittedRunContext, str]:
        if entry.ordinal < 2:
            raise ValueError("materialize_next_entry requires ordinal >= 2")
        expected = definition.entries[entry.ordinal - 1]
        if expected.planned_run_id != entry.planned_run_id:
            raise ValueError("entry planned_run_id disagrees with sequence definition")
        entry_hash = frozen_entry_hash(entry)
        context = build_sequence_run_context(
            definition=definition,
            entry=entry,
            entry_hash=entry_hash,
        )
        run_id = entry.planned_run_id
        self._validate_orphan_run_artifact_tree(run_id)
        self._copy_entry_artifacts(
            sequence_id=sequence_id,
            run_id=run_id,
            entry=entry,
        )
        return context, entry_hash

    def _validate_orphan_run_artifact_tree(self, run_id: str) -> None:
        try:
            self.artifacts.reject_unexpected_run_artifacts(
                run_id,
                allowed_relative_paths=SEQUENCE_MATERIALIZED_RUN_ARTIFACT_PATHS,
            )
        except ProtectedArtifactError as exc:
            raise ProtectedArtifactError(
                "unexpected run artifacts block sequence materialization"
            ) from exc

    def _copy_entry_artifacts(
        self,
        *,
        sequence_id: str,
        run_id: str,
        entry: FrozenSequenceEntry,
    ) -> None:
        copies = (
            (
                entry.plan_prompt.plan_artifact_path,
                PLAN_ARTIFACT,
                entry.plan_prompt.plan_sha256,
                MAX_PLAN_BYTES,
            ),
            (
                entry.plan_prompt.prompt_artifact_path,
                PROMPT_ARTIFACT,
                entry.plan_prompt.prompt_sha256,
                MAX_PROMPT_BYTES,
            ),
            (
                entry.effective_config.effective_config_artifact_path,
                EFFECTIVE_CONFIG_ARTIFACT,
                entry.effective_config.effective_config_sha256,
                MAX_CONFIG_BYTES,
            ),
            (
                entry.effective_config.source_config_artifact_path,
                SOURCE_CONFIG_ARTIFACT,
                entry.effective_config.source_config_sha256,
                MAX_CONFIG_BYTES,
            ),
            (
                entry.codex.binding_artifact_path,
                FRESH_REVIEWER_INPUT_ARTIFACT,
                entry.codex.binding_sha256,
                MAX_SESSION_RUNTIME_BYTES,
            ),
        )
        for source_path, destination_path, expected_sha256, max_bytes in copies:
            content = self.artifacts.read_sequence_verified_bytes(
                sequence_id,
                source_path,
                expected_sha256=expected_sha256,
            )
            try:
                self.artifacts.write_text_or_verify(
                    run_id,
                    destination_path,
                    content.decode("utf-8"),
                    max_bytes=max_bytes,
                )
            except ProtectedArtifactError as exc:
                raise ProtectedArtifactError(
                    "run artifact conflict while materializing sequence entry"
                ) from exc


def verify_orphaned_run_artifacts(
    artifacts: ProtectedArtifactStore,
    *,
    sequence_id: str,
    run_id: str,
    entry: FrozenSequenceEntry,
) -> None:
    materializer = SequenceRunMaterializer(artifacts)
    materializer._copy_entry_artifacts(
        sequence_id=sequence_id,
        run_id=run_id,
        entry=entry,
    )


def entry_hash_matches_payload(entry: FrozenSequenceEntry, payload_sha256_value: str) -> bool:
    return frozen_entry_hash(entry) == payload_sha256_value

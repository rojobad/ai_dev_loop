"""Authenticated artifact helpers for Phase 20.6 recovery."""

from __future__ import annotations

import hashlib
import json

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.recovery_worktree import RecoverySeedEvidence
from ai_dev_loop.scheduler.domain.common import canonical_json_sha256
from ai_dev_loop.scheduler.domain.recovery import (
    RECOVERY_CLEANUP_EVIDENCE_ARTIFACT,
    RECOVERY_DEFINITION_ARTIFACT,
    RECOVERY_INTEGRATION_INTENT_ARTIFACT,
    RECOVERY_INTEGRATION_RESULT_ARTIFACT,
    RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT,
    RECOVERY_SEED_EVIDENCE_ARTIFACT,
    RECOVERY_START_INTENT_ARTIFACT,
    FreshReviewRecoveryDefinition,
    RecoveryCheckpointTrustedTree,
    RecoveryIntegrationIntent,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
    StoredArtifact,
)

RECOVERY_SOURCE_STAGED_PATCH_ARTIFACT = "recovery/source-staged.patch"
RECOVERY_ADMISSION_STATUS_ARTIFACT = "recovery/admission.json"


def recovery_run_id_for(recovery_id: str) -> str:
    digest = hashlib.sha256(recovery_id.encode("utf-8")).hexdigest()
    return f"run-{digest[:24]}"


def definition_digest_binding(definition: FreshReviewRecoveryDefinition) -> str:
    return canonical_json_sha256(
        {
            "source_run_id": definition.source_run_id,
            "parent_head": definition.parent_head,
            "staged_patch_sha256": definition.source_staged_patch_sha256,
            "staged_tree_sha256": definition.source_staged_tree_sha256,
            "commit_message": definition.integration.commit_message,
            "plan_sha256": definition.plan_prompt.plan_sha256,
            "prompt_sha256": definition.plan_prompt.prompt_sha256,
            "sequence": definition.sequence.model_dump(mode="json")
            if definition.sequence
            else None,
        }
    )


def persist_recovery_definition(
    artifacts: ProtectedArtifactStore,
    recovery_id: str,
    definition: FreshReviewRecoveryDefinition,
) -> StoredArtifact:
    binding = definition_digest_binding(definition)
    if binding != definition.definition_sha256:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.INTERNAL,
            "definition digest binding mismatch",
        )
    definition_bytes = json.dumps(
        definition.model_dump(mode="json"), indent=2, sort_keys=True
    ).encode("utf-8")
    return artifacts.write_bytes(
        recovery_id,
        RECOVERY_DEFINITION_ARTIFACT,
        definition_bytes,
        max_bytes=len(definition_bytes) + 1,
    )


def load_recovery_definition(
    artifacts: ProtectedArtifactStore,
    recovery_id: str,
    *,
    definition_sha256: str,
    definition_artifact_sha256: str,
) -> FreshReviewRecoveryDefinition:
    try:
        raw = artifacts.read_verified_bytes(
            recovery_id,
            RECOVERY_DEFINITION_ARTIFACT,
            expected_sha256=definition_artifact_sha256,
        )
    except ProtectedArtifactError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            str(exc),
        ) from exc
    definition = FreshReviewRecoveryDefinition.model_validate_json(raw)
    if definition.definition_sha256 != definition_sha256:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "definition artifact digest binding mismatch",
        )
    return definition


def persist_recovery_start_intent(
    artifacts: ProtectedArtifactStore,
    recovery_id: str,
    *,
    definition_sha256: str,
    definition_artifact_sha256: str,
    recovery_run_id: str,
    requested_at: str,
) -> StoredArtifact:
    payload = {
        "recovery_id": recovery_id,
        "definition_sha256": definition_sha256,
        "definition_artifact_sha256": definition_artifact_sha256,
        "recovery_run_id": recovery_run_id,
        "requested_at": requested_at,
    }
    content = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    return artifacts.write_bytes(
        recovery_id,
        RECOVERY_START_INTENT_ARTIFACT,
        content,
        max_bytes=len(content) + 1,
    )


def load_recovery_seed_evidence(
    artifacts: ProtectedArtifactStore,
    recovery_id: str,
    *,
    definition: FreshReviewRecoveryDefinition,
    expected_sha256: str | None = None,
) -> RecoverySeedEvidence:
    try:
        if expected_sha256 is not None:
            raw = artifacts.read_verified_bytes(
                recovery_id,
                RECOVERY_SEED_EVIDENCE_ARTIFACT,
                expected_sha256=expected_sha256,
            )
        else:
            seed_path = artifacts.run_root(recovery_id) / RECOVERY_SEED_EVIDENCE_ARTIFACT
            if not seed_path.is_file():
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "recovery seed evidence is missing",
                )
            raw = seed_path.read_bytes()
    except ProtectedArtifactError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            str(exc),
        ) from exc
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery seed evidence is invalid",
        )
    seed = RecoverySeedEvidence(
        parent_head=str(payload["parent_head"]),
        staged_patch_sha256=str(payload["staged_patch_sha256"]),
        staged_tree_sha256=str(payload["staged_tree_sha256"]),
        worktree_path=str(payload["worktree_path"]),
        private_ref=str(payload["private_ref"]),
        target_repository_root=str(payload["target_repository_root"]),
        managed_git_common_dir=str(payload["managed_git_common_dir"]),
        managed_git_dir=str(payload["managed_git_dir"]),
        managed_branch=str(payload.get("managed_branch", "HEAD")),
        pre_seed_admission_status=str(payload.get("pre_seed_admission_status", "")),
        post_seed_status_porcelain=str(payload.get("post_seed_status_porcelain", "")),
    )
    if seed.parent_head != definition.parent_head:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery seed parent_head disagrees with definition",
        )
    if seed.staged_patch_sha256 != definition.source_staged_patch_sha256:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery seed patch digest disagrees with definition",
        )
    if seed.staged_tree_sha256 != definition.source_staged_tree_sha256:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery seed tree digest disagrees with definition",
        )
    if seed.private_ref != definition.private_ref:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery seed private_ref disagrees with definition",
        )
    if seed.target_repository_root != definition.repository.root:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery seed target repository disagrees with definition",
        )
    return seed


def load_recovery_integration_result(
    artifacts: ProtectedArtifactStore,
    recovery_id: str,
    *,
    expected_sha256: str | None = None,
) -> dict[str, str]:
    result_path = artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_RESULT_ARTIFACT
    if not result_path.is_file():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery integration result is missing",
        )
    if expected_sha256 is not None:
        raw = artifacts.read_verified_bytes(
            recovery_id,
            RECOVERY_INTEGRATION_RESULT_ARTIFACT,
            expected_sha256=expected_sha256,
        )
    else:
        raw = result_path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery integration result is invalid",
        )
    return {str(key): str(value) for key, value in payload.items()}


def load_recovery_integration_trusted_tree(
    artifacts: ProtectedArtifactStore,
    recovery_id: str,
    *,
    expected_sha256: str | None = None,
) -> tuple[RecoveryCheckpointTrustedTree, str]:
    trusted_path = artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT
    if not trusted_path.is_file():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery integration trusted-tree artifact is missing",
        )
    if expected_sha256 is not None:
        raw = artifacts.read_verified_bytes(
            recovery_id,
            RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT,
            expected_sha256=expected_sha256,
        )
    else:
        raw = trusted_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery integration trusted-tree digest mismatch",
        )
    trusted = RecoveryCheckpointTrustedTree.model_validate_json(raw)
    return trusted, digest


def load_recovery_cleanup_evidence(
    artifacts: ProtectedArtifactStore,
    recovery_id: str,
    *,
    expected_sha256: str,
    integrated_commit_sha256: str,
    worktree_path: str,
    private_ref: str,
) -> dict[str, str]:
    try:
        raw = artifacts.read_verified_bytes(
            recovery_id,
            RECOVERY_CLEANUP_EVIDENCE_ARTIFACT,
            expected_sha256=expected_sha256,
        )
    except ProtectedArtifactError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            str(exc),
        ) from exc
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery cleanup evidence is invalid",
        )
    evidence = {str(key): str(value) for key, value in payload.items()}
    if evidence.get("integrated_commit_sha256") != integrated_commit_sha256:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery cleanup evidence commit binding mismatch",
        )
    if evidence.get("worktree_path") != worktree_path:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery cleanup evidence worktree path mismatch",
        )
    if evidence.get("private_ref") != private_ref:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery cleanup evidence private ref mismatch",
        )
    return evidence


def load_recovery_integration_intent(
    artifacts: ProtectedArtifactStore,
    recovery_id: str,
    *,
    expected_sha256: str | None = None,
) -> RecoveryIntegrationIntent:
    intent_path = artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_INTENT_ARTIFACT
    if not intent_path.is_file():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery integration intent is missing",
        )
    raw = intent_path.read_bytes()
    if expected_sha256 is not None:
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery integration intent digest mismatch",
            )
    return RecoveryIntegrationIntent.model_validate_json(raw)


def load_recovery_start_intent(
    artifacts: ProtectedArtifactStore,
    recovery_id: str,
    *,
    expected_sha256: str,
) -> dict[str, str]:
    try:
        raw = artifacts.read_verified_bytes(
            recovery_id,
            RECOVERY_START_INTENT_ARTIFACT,
            expected_sha256=expected_sha256,
        )
    except ProtectedArtifactError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            str(exc),
        ) from exc
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery start intent is invalid",
        )
    return {str(key): str(value) for key, value in payload.items()}

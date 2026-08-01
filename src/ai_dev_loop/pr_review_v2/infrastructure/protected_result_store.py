"""Protected run-scoped store for PR review v2 local execution artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, TypeVar

from ai_dev_loop.paths import DIR_MODE, SENSITIVE_FILE_MODE, ensure_dir, set_sensitive_file_mode
from ai_dev_loop.pr_review_v2.application.contracts import AppModel
from ai_dev_loop.pr_review_v2.application.control_contracts import (
    MixedAdjudicationRecoveryArtifact,
    OperatorContinuationArtifact,
)
from ai_dev_loop.pr_review_v2.application.execution_context import (
    ExecutionContextArtifact,
    ExternalAdjudicationResultArtifact,
    LocalFixResultArtifact,
    PublicationGenerationResultArtifact,
)
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    DEFAULT_MAX_COMMIT_MESSAGE_BYTES,
    DEFAULT_MAX_PATCH_BYTES,
    DEFAULT_MAX_TEXT_BYTES,
    AdoptedExistingPrPreimageArtifact,
    CommitMessageArtifact,
    PublicationTextArtifact,
    reject_prohibited_controls,
)
from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef, LocalFixOutcomeKind
from ai_dev_loop.pr_review_v2.domain.effects import (
    AdjudicateThreadsEffect,
    GeneratePublicationTextEffect,
    RunLocalFixEffect,
)
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import (
    InputArtifactError,
    InputArtifactErrorKind,
    InputArtifactReader,
    read_content_commitment_digest,
    read_verified_bytes_under_run_root,
    read_verified_sensitive_bytes,
)
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    ensure_run_artifact_root,
    resolve_run_relative_path,
    run_artifact_root,
)

_TArtifact = TypeVar("_TArtifact", bound=AppModel)

EXECUTION_CONTEXT_DIR = "local/execution-context"
RESULTS_DIR = "local/results"
PATCHES_DIR = "local/patches"
REPLIES_DIR = "local/replies"
OPERATOR_CONTINUATION_DIR = "local/operator-continuation"
MIXED_ADJUDICATION_RECOVERY_DIR = "local/mixed-adjudication-recovery"
PUBLICATION_GENERATION_DIR = "local/results/publication-generation"
ADJUDICATION_RESULT_DIR = "local/results/adjudication"
LOCAL_FIX_RESULT_DIR = "local/results/local-fix"
PUBLICATION_TEXT_DIR = "local/publication-text"
COMMIT_MESSAGE_DIR = "local/commit-message"
SOURCE_PLAN_RELATIVE = "local/source/plan.md"
SOURCE_PROMPT_RELATIVE = "local/source/prompt.txt"
ADOPTED_EXISTING_PR_PREIMAGE_RELATIVE = "local/adopted-existing-pr-preimage.json"
# Legacy fixed paths retained for older fixtures; new writes are effect-keyed.
PUBLICATION_TEXT_RELATIVE = "local/publication-text.json"
COMMIT_MESSAGE_RELATIVE = "local/commit-message.json"
FIX_PROMPT_RELATIVE = "local/fix-prompt.txt"
FIX_PROMPTS_DIR = "local/fix-prompts"

MAX_PROTECTED_RESULT_JSON_BYTES = 1_048_576
MAX_SOURCE_PLAN_BYTES = DEFAULT_MAX_TEXT_BYTES
MAX_SOURCE_PROMPT_BYTES = DEFAULT_MAX_TEXT_BYTES
MAX_FIX_PROMPT_BYTES = DEFAULT_MAX_TEXT_BYTES
MAX_REPLY_TEXT_BYTES = DEFAULT_MAX_TEXT_BYTES

_SAFE_REPLY_HINT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _effect_key(effect_id: str) -> str:
    return hashlib.sha256(effect_id.encode("utf-8")).hexdigest()[:32]


def _publication_binding_key(
    *,
    run_id: str,
    effect_id: str,
    cycle_number: int,
    bound_head_sha: str,
    evidence_ref_sha256: str,
    patch_ref_sha256: str,
) -> str:
    material = (
        f"{run_id}|{effect_id}|{cycle_number}|{bound_head_sha.lower()}|"
        f"{evidence_ref_sha256}|{patch_ref_sha256}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _adjudication_binding_key(
    *,
    run_id: str,
    effect_id: str,
    cycle_number: int,
    bound_head_sha: str,
    snapshot_ref_sha256: str,
    execution_context_ref_sha256: str,
    frozen_thread_ids: tuple[str, ...],
) -> str:
    threads = ",".join(sorted(frozen_thread_ids))
    material = (
        f"{run_id}|{effect_id}|{cycle_number}|{bound_head_sha.lower()}|"
        f"{snapshot_ref_sha256}|{execution_context_ref_sha256}|{threads}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _local_fix_binding_key(
    *,
    run_id: str,
    effect_id: str,
    cycle_number: int,
    bound_head_sha: str,
    fix_prompt_ref_sha256: str,
    execution_context_ref_sha256: str,
) -> str:
    material = (
        f"{run_id}|{effect_id}|{cycle_number}|{bound_head_sha.lower()}|"
        f"{fix_prompt_ref_sha256}|{execution_context_ref_sha256}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def adjudication_result_commitment_relative(
    *,
    run_id: str,
    effect_id: str,
    cycle_number: int,
    bound_head_sha: str,
    snapshot_ref_sha256: str,
    execution_context_ref_sha256: str,
    frozen_thread_ids: tuple[str, ...],
) -> str:
    key = _adjudication_binding_key(
        run_id=run_id,
        effect_id=effect_id,
        cycle_number=cycle_number,
        bound_head_sha=bound_head_sha,
        snapshot_ref_sha256=snapshot_ref_sha256,
        execution_context_ref_sha256=execution_context_ref_sha256,
        frozen_thread_ids=frozen_thread_ids,
    )
    return f"{ADJUDICATION_RESULT_DIR}/{key}.commit"


def adjudication_result_relative(
    *,
    run_id: str,
    effect_id: str,
    cycle_number: int,
    bound_head_sha: str,
    snapshot_ref_sha256: str,
    execution_context_ref_sha256: str,
    frozen_thread_ids: tuple[str, ...],
) -> str:
    """Legacy name: binding commitment path for effect-bound adjudication lookup."""

    return adjudication_result_commitment_relative(
        run_id=run_id,
        effect_id=effect_id,
        cycle_number=cycle_number,
        bound_head_sha=bound_head_sha,
        snapshot_ref_sha256=snapshot_ref_sha256,
        execution_context_ref_sha256=execution_context_ref_sha256,
        frozen_thread_ids=frozen_thread_ids,
    )


def local_fix_result_commitment_relative(
    *,
    run_id: str,
    effect_id: str,
    cycle_number: int,
    bound_head_sha: str,
    fix_prompt_ref_sha256: str,
    execution_context_ref_sha256: str,
) -> str:
    key = _local_fix_binding_key(
        run_id=run_id,
        effect_id=effect_id,
        cycle_number=cycle_number,
        bound_head_sha=bound_head_sha,
        fix_prompt_ref_sha256=fix_prompt_ref_sha256,
        execution_context_ref_sha256=execution_context_ref_sha256,
    )
    return f"{LOCAL_FIX_RESULT_DIR}/{key}.commit"


def local_fix_result_relative(
    *,
    run_id: str,
    effect_id: str,
    cycle_number: int,
    bound_head_sha: str,
    fix_prompt_ref_sha256: str,
    execution_context_ref_sha256: str,
) -> str:
    """Legacy name: binding commitment path for effect-bound local-fix lookup."""

    return local_fix_result_commitment_relative(
        run_id=run_id,
        effect_id=effect_id,
        cycle_number=cycle_number,
        bound_head_sha=bound_head_sha,
        fix_prompt_ref_sha256=fix_prompt_ref_sha256,
        execution_context_ref_sha256=execution_context_ref_sha256,
    )


def publication_generation_commitment_relative(
    *,
    run_id: str,
    effect_id: str,
    cycle_number: int,
    bound_head_sha: str,
    evidence_ref_sha256: str,
    patch_ref_sha256: str,
) -> str:
    key = _publication_binding_key(
        run_id=run_id,
        effect_id=effect_id,
        cycle_number=cycle_number,
        bound_head_sha=bound_head_sha,
        evidence_ref_sha256=evidence_ref_sha256,
        patch_ref_sha256=patch_ref_sha256,
    )
    return f"{PUBLICATION_GENERATION_DIR}/{key}.commit"


def publication_generation_relative(
    *,
    run_id: str,
    effect_id: str,
    cycle_number: int,
    bound_head_sha: str,
    evidence_ref_sha256: str,
    patch_ref_sha256: str,
) -> str:
    """Legacy name: binding commitment path for effect-bound publication lookup."""

    return publication_generation_commitment_relative(
        run_id=run_id,
        effect_id=effect_id,
        cycle_number=cycle_number,
        bound_head_sha=bound_head_sha,
        evidence_ref_sha256=evidence_ref_sha256,
        patch_ref_sha256=patch_ref_sha256,
    )


def publication_text_commitment_relative(
    *,
    run_id: str,
    effect_id: str,
    cycle_number: int,
    bound_head_sha: str,
    evidence_ref_sha256: str,
    patch_ref_sha256: str,
) -> str:
    key = _publication_binding_key(
        run_id=run_id,
        effect_id=effect_id,
        cycle_number=cycle_number,
        bound_head_sha=bound_head_sha,
        evidence_ref_sha256=evidence_ref_sha256,
        patch_ref_sha256=patch_ref_sha256,
    )
    return f"{PUBLICATION_TEXT_DIR}/{key}.commit"


def publication_text_relative(
    *,
    run_id: str,
    effect_id: str,
    cycle_number: int,
    bound_head_sha: str,
    evidence_ref_sha256: str,
    patch_ref_sha256: str,
) -> str:
    return publication_text_commitment_relative(
        run_id=run_id,
        effect_id=effect_id,
        cycle_number=cycle_number,
        bound_head_sha=bound_head_sha,
        evidence_ref_sha256=evidence_ref_sha256,
        patch_ref_sha256=patch_ref_sha256,
    )


def commit_message_commitment_relative(
    *,
    run_id: str,
    effect_id: str,
    cycle_number: int,
    bound_head_sha: str,
    evidence_ref_sha256: str,
    patch_ref_sha256: str,
) -> str:
    key = _publication_binding_key(
        run_id=run_id,
        effect_id=effect_id,
        cycle_number=cycle_number,
        bound_head_sha=bound_head_sha,
        evidence_ref_sha256=evidence_ref_sha256,
        patch_ref_sha256=patch_ref_sha256,
    )
    return f"{COMMIT_MESSAGE_DIR}/{key}.commit"


def commit_message_relative(
    *,
    run_id: str,
    effect_id: str,
    cycle_number: int,
    bound_head_sha: str,
    evidence_ref_sha256: str,
    patch_ref_sha256: str,
) -> str:
    return commit_message_commitment_relative(
        run_id=run_id,
        effect_id=effect_id,
        cycle_number=cycle_number,
        bound_head_sha=bound_head_sha,
        evidence_ref_sha256=evidence_ref_sha256,
        patch_ref_sha256=patch_ref_sha256,
    )


def _content_relative(directory: str, digest: str) -> str:
    return f"{directory}/{digest}.json"


class ProtectedResultStoreError(Exception):
    """Raised when protected local-result persistence or verification fails."""

    def __init__(self, message: str = "protected result persistence failed") -> None:
        super().__init__(message)


class ProtectedResultStore:
    """Atomic protected writer/reader for Phase 16.7 local execution artifacts."""

    def __init__(self, artifact_root: Path) -> None:
        self._root = artifact_root

    @property
    def root(self) -> Path:
        return self._root

    def persist_execution_context(
        self, *, run_id: str, artifact: ExecutionContextArtifact
    ) -> ArtifactRef:
        payload = artifact.model_dump(mode="json")
        return self._persist_json(
            run_id=run_id,
            relative_prefix=EXECUTION_CONTEXT_DIR,
            payload=payload,
        )

    def read_execution_context(self, *, run_id: str, ref: ArtifactRef) -> ExecutionContextArtifact:
        return self._read_json(run_id=run_id, ref=ref, model=ExecutionContextArtifact)

    def persist_publication_generation(
        self, *, run_id: str, artifact: PublicationGenerationResultArtifact
    ) -> ArtifactRef:
        payload = artifact.model_dump(mode="json")
        commitment = publication_generation_commitment_relative(
            run_id=artifact.run_id,
            effect_id=artifact.effect_id,
            cycle_number=artifact.cycle_number,
            bound_head_sha=artifact.bound_head_sha,
            evidence_ref_sha256=artifact.evidence_ref_sha256,
            patch_ref_sha256=artifact.patch_ref_sha256,
        )
        return self._persist_effect_bound_json(
            run_id=run_id,
            directory=PUBLICATION_GENERATION_DIR,
            commitment_relative=commitment,
            payload=payload,
        )

    def read_publication_generation(
        self, *, run_id: str, ref: ArtifactRef
    ) -> PublicationGenerationResultArtifact:
        return self._read_json(run_id=run_id, ref=ref, model=PublicationGenerationResultArtifact)

    def resolve_cached_publication_generation_ref(
        self, effect: GeneratePublicationTextEffect
    ) -> ArtifactRef | None:
        commitment = publication_generation_commitment_relative(
            run_id=effect.run_id,
            effect_id=effect.effect_id,
            cycle_number=effect.cycle_number,
            bound_head_sha=effect.bound_head_sha,
            evidence_ref_sha256=effect.evidence_ref.sha256,
            patch_ref_sha256=effect.patch_ref.sha256,
        )
        return self._resolve_commitment_ref(
            run_id=effect.run_id,
            directory=PUBLICATION_GENERATION_DIR,
            commitment_relative=commitment,
        )

    def read_cached_publication_generation(
        self, effect: GeneratePublicationTextEffect
    ) -> PublicationGenerationResultArtifact | None:
        """Return effect-bound generation result only when all bindings match."""

        ref = self.resolve_cached_publication_generation_ref(effect)
        if ref is None:
            return None
        try:
            artifact = self.read_publication_generation(run_id=effect.run_id, ref=ref)
        except ProtectedResultStoreError:
            return None
        if (
            artifact.run_id != effect.run_id
            or artifact.effect_id != effect.effect_id
            or artifact.cycle_number != effect.cycle_number
            or artifact.bound_head_sha.lower() != effect.bound_head_sha.lower()
            or artifact.evidence_ref_sha256 != effect.evidence_ref.sha256
            or artifact.patch_ref_sha256 != effect.patch_ref.sha256
        ):
            return None
        return artifact

    def persist_external_adjudication(
        self, *, run_id: str, artifact: ExternalAdjudicationResultArtifact
    ) -> ArtifactRef:
        payload = artifact.model_dump(mode="json")
        commitment = adjudication_result_commitment_relative(
            run_id=artifact.run_id,
            effect_id=artifact.effect_id,
            cycle_number=artifact.cycle_number,
            bound_head_sha=artifact.bound_head_sha,
            snapshot_ref_sha256=artifact.snapshot_ref_sha256,
            execution_context_ref_sha256=artifact.execution_context_ref_sha256,
            frozen_thread_ids=tuple(artifact.frozen_thread_ids),
        )
        return self._persist_effect_bound_json(
            run_id=run_id,
            directory=ADJUDICATION_RESULT_DIR,
            commitment_relative=commitment,
            payload=payload,
        )

    def read_external_adjudication(
        self, *, run_id: str, ref: ArtifactRef
    ) -> ExternalAdjudicationResultArtifact:
        return self._read_json(run_id=run_id, ref=ref, model=ExternalAdjudicationResultArtifact)

    def resolve_cached_external_adjudication_ref(
        self, effect: AdjudicateThreadsEffect
    ) -> ArtifactRef | None:
        commitment = adjudication_result_commitment_relative(
            run_id=effect.run_id,
            effect_id=effect.effect_id,
            cycle_number=effect.cycle_number,
            bound_head_sha=effect.bound_head_sha,
            snapshot_ref_sha256=effect.snapshot_ref.sha256,
            execution_context_ref_sha256=effect.execution_context_ref.sha256,
            frozen_thread_ids=tuple(effect.frozen_thread_ids),
        )
        return self._resolve_commitment_ref(
            run_id=effect.run_id,
            directory=ADJUDICATION_RESULT_DIR,
            commitment_relative=commitment,
        )

    def read_cached_external_adjudication(
        self, effect: AdjudicateThreadsEffect
    ) -> ExternalAdjudicationResultArtifact | None:
        """Return effect-bound adjudication only when every binding matches."""

        ref = self.resolve_cached_external_adjudication_ref(effect)
        if ref is None:
            return None
        try:
            artifact = self.read_external_adjudication(run_id=effect.run_id, ref=ref)
        except ProtectedResultStoreError:
            return None
        if (
            artifact.run_id != effect.run_id
            or artifact.effect_id != effect.effect_id
            or artifact.cycle_number != effect.cycle_number
            or artifact.bound_head_sha.lower() != effect.bound_head_sha.lower()
            or artifact.snapshot_ref_sha256 != effect.snapshot_ref.sha256
            or artifact.execution_context_ref_sha256 != effect.execution_context_ref.sha256
            or set(artifact.frozen_thread_ids) != set(effect.frozen_thread_ids)
        ):
            return None
        return artifact

    def persist_local_fix_result(
        self, *, run_id: str, artifact: LocalFixResultArtifact
    ) -> ArtifactRef:
        payload = artifact.model_dump(mode="json")
        commitment = local_fix_result_commitment_relative(
            run_id=artifact.run_id,
            effect_id=artifact.effect_id,
            cycle_number=artifact.cycle_number,
            bound_head_sha=artifact.bound_head_sha,
            fix_prompt_ref_sha256=artifact.fix_prompt_ref_sha256,
            execution_context_ref_sha256=artifact.execution_context_ref_sha256,
        )
        return self._persist_effect_bound_json(
            run_id=run_id,
            directory=LOCAL_FIX_RESULT_DIR,
            commitment_relative=commitment,
            payload=payload,
        )

    def read_local_fix_result(self, *, run_id: str, ref: ArtifactRef) -> LocalFixResultArtifact:
        return self._read_json(run_id=run_id, ref=ref, model=LocalFixResultArtifact)

    def resolve_cached_local_fix_result_ref(self, effect: RunLocalFixEffect) -> ArtifactRef | None:
        commitment = local_fix_result_commitment_relative(
            run_id=effect.run_id,
            effect_id=effect.effect_id,
            cycle_number=effect.cycle_number,
            bound_head_sha=effect.bound_head_sha,
            fix_prompt_ref_sha256=effect.fix_prompt_ref.sha256,
            execution_context_ref_sha256=effect.execution_context_ref.sha256,
        )
        return self._resolve_commitment_ref(
            run_id=effect.run_id,
            directory=LOCAL_FIX_RESULT_DIR,
            commitment_relative=commitment,
        )

    def read_cached_local_fix_result(
        self, effect: RunLocalFixEffect
    ) -> LocalFixResultArtifact | None:
        """Return effect-bound local-fix result only when every binding matches."""

        ref = self.resolve_cached_local_fix_result_ref(effect)
        if ref is None:
            return None
        try:
            artifact = self.read_local_fix_result(run_id=effect.run_id, ref=ref)
        except ProtectedResultStoreError:
            return None
        if (
            artifact.run_id != effect.run_id
            or artifact.effect_id != effect.effect_id
            or artifact.cycle_number != effect.cycle_number
            or artifact.bound_head_sha.lower() != effect.bound_head_sha.lower()
            or artifact.fix_prompt_ref_sha256 != effect.fix_prompt_ref.sha256
            or artifact.execution_context_ref_sha256 != effect.execution_context_ref.sha256
        ):
            return None
        return artifact

    def persist_source_plan_bytes(self, *, run_id: str, data: bytes) -> ArtifactRef:
        if not data:
            raise ProtectedResultStoreError("source plan bytes must not be empty")
        return self._persist_bytes(
            run_id=run_id,
            relative_path=SOURCE_PLAN_RELATIVE,
            data=data,
            max_bytes=MAX_SOURCE_PLAN_BYTES,
        )

    def persist_source_prompt_bytes(self, *, run_id: str, data: bytes) -> ArtifactRef:
        if not data.strip():
            raise ProtectedResultStoreError("source prompt bytes must not be empty")
        return self._persist_bytes(
            run_id=run_id,
            relative_path=SOURCE_PROMPT_RELATIVE,
            data=data,
            max_bytes=MAX_SOURCE_PROMPT_BYTES,
        )

    def persist_adopted_existing_pr_preimage(
        self, *, run_id: str, artifact: AdoptedExistingPrPreimageArtifact
    ) -> ArtifactRef:
        """Persist the prepare-time existing-PR title/body preimage (owner-only)."""

        title_bytes = artifact.title.encode("utf-8")
        body_bytes = artifact.body.encode("utf-8")
        if len(title_bytes) > DEFAULT_MAX_TEXT_BYTES or len(body_bytes) > DEFAULT_MAX_TEXT_BYTES:
            raise ProtectedResultStoreError("artifact exceeds maximum size")
        return self._persist_deterministic_json(
            run_id=run_id,
            relative_path=ADOPTED_EXISTING_PR_PREIMAGE_RELATIVE,
            payload=artifact.model_dump(mode="json"),
            max_bytes=MAX_PROTECTED_RESULT_JSON_BYTES,
        )

    def read_adopted_existing_pr_preimage(
        self, *, run_id: str, ref: ArtifactRef
    ) -> AdoptedExistingPrPreimageArtifact:
        return self._read_json(run_id=run_id, ref=ref, model=AdoptedExistingPrPreimageArtifact)

    def read_source_plan_bytes(self, *, run_id: str, expected_sha256: str) -> bytes:
        return self._read_bound_bytes(
            run_id=run_id,
            relative_path=SOURCE_PLAN_RELATIVE,
            expected_sha256=expected_sha256,
            max_bytes=MAX_SOURCE_PLAN_BYTES,
        )

    def read_source_prompt_bytes(self, *, run_id: str, expected_sha256: str) -> bytes:
        return self._read_bound_bytes(
            run_id=run_id,
            relative_path=SOURCE_PROMPT_RELATIVE,
            expected_sha256=expected_sha256,
            max_bytes=MAX_SOURCE_PROMPT_BYTES,
        )

    def _read_bound_bytes(
        self, *, run_id: str, relative_path: str, expected_sha256: str, max_bytes: int
    ) -> bytes:
        try:
            return read_verified_sensitive_bytes(
                self._root,
                run_id=run_id,
                relative_path=relative_path,
                expected_sha256=expected_sha256,
                max_bytes=max_bytes,
            )
        except InputArtifactError as exc:
            raise self._map_input_error(exc, prefix="source snapshot") from exc

    def persist_operator_continuation(
        self, *, run_id: str, artifact: OperatorContinuationArtifact
    ) -> ArtifactRef:
        payload = artifact.model_dump(mode="json")
        return self._persist_json(
            run_id=run_id,
            relative_prefix=OPERATOR_CONTINUATION_DIR,
            payload=payload,
        )

    def persist_mixed_adjudication_recovery(
        self, *, run_id: str, artifact: MixedAdjudicationRecoveryArtifact
    ) -> ArtifactRef:
        payload = artifact.model_dump(mode="json")
        return self._persist_json(
            run_id=run_id,
            relative_prefix=MIXED_ADJUDICATION_RECOVERY_DIR,
            payload=payload,
        )

    def read_operator_continuation(
        self, *, run_id: str, ref: ArtifactRef
    ) -> OperatorContinuationArtifact:
        return self._read_json(run_id=run_id, ref=ref, model=OperatorContinuationArtifact)

    def persist_publication_text_and_commit_message(
        self,
        *,
        run_id: str,
        title: str,
        body: str,
        subject: str,
        commit_body: str,
        effect_id: str | None = None,
        cycle_number: int | None = None,
        bound_head_sha: str | None = None,
        evidence_ref_sha256: str | None = None,
        patch_ref_sha256: str | None = None,
    ) -> tuple[ArtifactRef, ArtifactRef]:
        publication = PublicationTextArtifact(title=title, body=body)
        commit_message = CommitMessageArtifact(subject=subject, body=commit_body)
        if (
            effect_id is not None
            and cycle_number is not None
            and bound_head_sha is not None
            and evidence_ref_sha256 is not None
            and patch_ref_sha256 is not None
        ):
            pub_commit = publication_text_commitment_relative(
                run_id=run_id,
                effect_id=effect_id,
                cycle_number=cycle_number,
                bound_head_sha=bound_head_sha,
                evidence_ref_sha256=evidence_ref_sha256,
                patch_ref_sha256=patch_ref_sha256,
            )
            commit_commit = commit_message_commitment_relative(
                run_id=run_id,
                effect_id=effect_id,
                cycle_number=cycle_number,
                bound_head_sha=bound_head_sha,
                evidence_ref_sha256=evidence_ref_sha256,
                patch_ref_sha256=patch_ref_sha256,
            )
            pub_ref = self._persist_effect_bound_json(
                run_id=run_id,
                directory=PUBLICATION_TEXT_DIR,
                commitment_relative=pub_commit,
                payload=publication.model_dump(mode="json"),
                max_bytes=DEFAULT_MAX_TEXT_BYTES,
            )
            commit_ref = self._persist_effect_bound_json(
                run_id=run_id,
                directory=COMMIT_MESSAGE_DIR,
                commitment_relative=commit_commit,
                payload=commit_message.model_dump(mode="json"),
                max_bytes=DEFAULT_MAX_COMMIT_MESSAGE_BYTES,
            )
            return pub_ref, commit_ref
        pub_ref = self._persist_deterministic_json(
            run_id=run_id,
            relative_path=PUBLICATION_TEXT_RELATIVE,
            payload=publication.model_dump(mode="json"),
            max_bytes=DEFAULT_MAX_TEXT_BYTES,
        )
        commit_ref = self._persist_deterministic_json(
            run_id=run_id,
            relative_path=COMMIT_MESSAGE_RELATIVE,
            payload=commit_message.model_dump(mode="json"),
            max_bytes=DEFAULT_MAX_COMMIT_MESSAGE_BYTES,
        )
        return pub_ref, commit_ref

    def persist_reply_text(self, *, run_id: str, relative_hint: str, text: str) -> ArtifactRef:
        # relative_hint retained for API stability; path is content-addressed so
        # distinct cycle bodies never collide on the same thread-id key.
        if relative_hint and not _SAFE_REPLY_HINT.match(relative_hint):
            raise ProtectedResultStoreError("reply relative_hint is unsafe")
        reject_prohibited_controls(text, field_name="reply text")
        if not text.strip():
            raise ProtectedResultStoreError("reply text must not be empty")
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        relative = f"{REPLIES_DIR}/{digest}.txt"
        return self._persist_bytes(
            run_id=run_id,
            relative_path=relative,
            data=data,
            max_bytes=MAX_REPLY_TEXT_BYTES,
        )

    def persist_fix_prompt(self, *, run_id: str, text: str) -> ArtifactRef:
        reject_prohibited_controls(text, field_name="fix prompt")
        if not text.strip():
            raise ProtectedResultStoreError("fix prompt must not be empty")
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        relative = f"{FIX_PROMPTS_DIR}/{digest}.txt"
        return self._persist_bytes(
            run_id=run_id,
            relative_path=relative,
            data=data,
            max_bytes=MAX_FIX_PROMPT_BYTES,
        )

    def latest_accepted_cursor_chat_id(self, *, run_id: str, before_cycle: int) -> str | None:
        """Return the exact chat from the latest prior accepted local-fix result."""

        if before_cycle <= 1:
            return None
        run_root = run_artifact_root(self._root, run_id)
        result_dir = run_root / LOCAL_FIX_RESULT_DIR
        if not result_dir.is_dir():
            return None
        best_cycle = 0
        best_chat: str | None = None
        for path in sorted(result_dir.glob("*.commit")):
            if not path.is_file() or path.is_symlink():
                continue
            commitment_relative = f"{LOCAL_FIX_RESULT_DIR}/{path.name}"
            ref = self._resolve_commitment_ref(
                run_id=run_id,
                directory=LOCAL_FIX_RESULT_DIR,
                commitment_relative=commitment_relative,
            )
            if ref is None:
                continue
            try:
                artifact = self.read_local_fix_result(run_id=run_id, ref=ref)
            except ProtectedResultStoreError:
                continue
            if artifact.run_id != run_id:
                continue
            if artifact.cycle_number >= before_cycle:
                continue
            if artifact.outcome not in {
                LocalFixOutcomeKind.ACCEPTED,
                LocalFixOutcomeKind.ACCEPTED_WITH_RESIDUAL_RISK,
            }:
                continue
            if not artifact.cursor_chat_id:
                continue
            if artifact.cycle_number > best_cycle:
                best_cycle = artifact.cycle_number
                best_chat = artifact.cursor_chat_id
        return best_chat

    def persist_patch_bytes(self, *, run_id: str, data: bytes) -> ArtifactRef:
        if not data:
            raise ProtectedResultStoreError("patch bytes must not be empty")
        if len(data) > DEFAULT_MAX_PATCH_BYTES:
            raise ProtectedResultStoreError("patch bytes exceed size bound")
        digest = hashlib.sha256(data).hexdigest()
        relative = f"{PATCHES_DIR}/{digest}.patch"
        return self._persist_bytes(run_id=run_id, relative_path=relative, data=data)

    def copy_verified_artifact(
        self,
        *,
        source_run_root: Path,
        source_ref: ArtifactRef,
        dest_run_id: str,
        dest_relative: str,
    ) -> ArtifactRef:
        try:
            raw = read_verified_bytes_under_run_root(
                source_run_root,
                source_ref.relative_path,
                expected_sha256=source_ref.sha256,
                max_bytes=MAX_PROTECTED_RESULT_JSON_BYTES,
            )
            digest = hashlib.sha256(raw).hexdigest()
            if digest != source_ref.sha256:
                raise ProtectedResultStoreError("source artifact hash mismatch")
            dest_root = ensure_run_artifact_root(self._root, dest_run_id)
            target = resolve_run_relative_path(dest_root, dest_relative)
            ensure_dir(target.parent, mode=DIR_MODE)
            if target.exists():
                existing = read_verified_sensitive_bytes(
                    self._root,
                    run_id=dest_run_id,
                    relative_path=dest_relative,
                    expected_sha256=digest,
                    max_bytes=MAX_PROTECTED_RESULT_JSON_BYTES,
                )
                if existing != raw:
                    raise ProtectedResultStoreError(
                        "destination artifact collision with different bytes"
                    )
                return ArtifactRef(relative_path=dest_relative, sha256=digest)
            _atomic_write_bytes_exclusive(target, raw)
            verified = read_verified_sensitive_bytes(
                self._root,
                run_id=dest_run_id,
                relative_path=dest_relative,
                expected_sha256=digest,
                max_bytes=MAX_PROTECTED_RESULT_JSON_BYTES,
            )
            if hashlib.sha256(verified).hexdigest() != digest:
                raise ProtectedResultStoreError("post-copy artifact hash mismatch")
            return ArtifactRef(relative_path=dest_relative, sha256=digest)
        except ProtectedResultStoreError:
            raise
        except InputArtifactError as exc:
            raise self._map_input_error(exc, prefix="source") from exc
        except OSError as exc:
            raise ProtectedResultStoreError("filesystem failure while copying artifact") from exc
        except ValueError as exc:
            raise ProtectedResultStoreError("artifact path is unsafe") from exc

    def verify_publication_readable(
        self, *, run_id: str, publication_ref: ArtifactRef, commit_ref: ArtifactRef
    ) -> None:
        reader = InputArtifactReader(self._root)
        reader.read_publication_text(run_id=run_id, ref=publication_ref)
        reader.read_commit_message(run_id=run_id, ref=commit_ref)

    def _persist_json(
        self, *, run_id: str, relative_prefix: str, payload: dict[str, Any]
    ) -> ArtifactRef:
        canonical = _canonical_json_bytes(payload)
        self._reject_oversized(canonical, max_bytes=MAX_PROTECTED_RESULT_JSON_BYTES)
        digest = hashlib.sha256(canonical).hexdigest()
        relative = f"{relative_prefix}/{digest}.json"
        return self._persist_canonical_bytes(run_id=run_id, relative_path=relative, data=canonical)

    def _persist_effect_bound_json(
        self,
        *,
        run_id: str,
        directory: str,
        commitment_relative: str,
        payload: dict[str, Any],
        max_bytes: int = MAX_PROTECTED_RESULT_JSON_BYTES,
    ) -> ArtifactRef:
        canonical = _canonical_json_bytes(payload)
        self._reject_oversized(canonical, max_bytes=max_bytes)
        digest = hashlib.sha256(canonical).hexdigest()
        content_relative = _content_relative(directory, digest)
        content_ref = self._persist_canonical_bytes(
            run_id=run_id, relative_path=content_relative, data=canonical
        )
        commitment_bytes = f"{digest}\n".encode("ascii")
        self._persist_canonical_bytes(
            run_id=run_id, relative_path=commitment_relative, data=commitment_bytes
        )
        return content_ref

    def _resolve_commitment_ref(
        self,
        *,
        run_id: str,
        directory: str,
        commitment_relative: str,
    ) -> ArtifactRef | None:
        try:
            digest = read_content_commitment_digest(
                self._root,
                run_id=run_id,
                relative_path=commitment_relative,
            )
        except InputArtifactError:
            return None
        content_relative = _content_relative(directory, digest)
        try:
            read_verified_sensitive_bytes(
                self._root,
                run_id=run_id,
                relative_path=content_relative,
                expected_sha256=digest,
                max_bytes=MAX_PROTECTED_RESULT_JSON_BYTES,
            )
        except InputArtifactError:
            return None
        return ArtifactRef(relative_path=content_relative, sha256=digest)

    def _persist_deterministic_json(
        self,
        *,
        run_id: str,
        relative_path: str,
        payload: dict[str, Any],
        max_bytes: int = MAX_PROTECTED_RESULT_JSON_BYTES,
    ) -> ArtifactRef:
        canonical = _canonical_json_bytes(payload)
        self._reject_oversized(canonical, max_bytes=max_bytes)
        return self._persist_canonical_bytes(
            run_id=run_id, relative_path=relative_path, data=canonical
        )

    def _persist_bytes(
        self,
        *,
        run_id: str,
        relative_path: str,
        data: bytes,
        max_bytes: int | None = None,
    ) -> ArtifactRef:
        if max_bytes is not None:
            self._reject_oversized(data, max_bytes=max_bytes)
        digest = hashlib.sha256(data).hexdigest()
        self._persist_canonical_bytes(run_id=run_id, relative_path=relative_path, data=data)
        return ArtifactRef(relative_path=relative_path, sha256=digest)

    def _persist_canonical_bytes(
        self, *, run_id: str, relative_path: str, data: bytes
    ) -> ArtifactRef:
        try:
            run_root = ensure_run_artifact_root(self._root, run_id)
            target = resolve_run_relative_path(run_root, relative_path)
            ensure_dir(target.parent, mode=DIR_MODE)
            digest = hashlib.sha256(data).hexdigest()
            if target.exists():
                existing = read_verified_sensitive_bytes(
                    self._root,
                    run_id=run_id,
                    relative_path=relative_path,
                    expected_sha256=digest,
                    max_bytes=max(len(data), 1),
                )
                if existing != data:
                    raise ProtectedResultStoreError("artifact path collision with different bytes")
                return ArtifactRef(relative_path=relative_path, sha256=digest)
            _atomic_write_bytes_exclusive(target, data)
            verified = read_verified_sensitive_bytes(
                self._root,
                run_id=run_id,
                relative_path=relative_path,
                expected_sha256=digest,
                max_bytes=max(len(data), 1),
            )
            if hashlib.sha256(verified).hexdigest() != digest:
                raise ProtectedResultStoreError("post-write artifact hash mismatch")
            return ArtifactRef(relative_path=relative_path, sha256=digest)
        except ProtectedResultStoreError:
            raise
        except InputArtifactError as exc:
            # Existing target with matching digest but unsafe mode must fail closed.
            raise self._map_input_error(exc) from exc
        except OSError as exc:
            raise ProtectedResultStoreError("filesystem failure while writing artifact") from exc
        except ValueError as exc:
            raise ProtectedResultStoreError("artifact path is unsafe") from exc

    def _read_json(self, *, run_id: str, ref: ArtifactRef, model: type[_TArtifact]) -> _TArtifact:
        try:
            raw = read_verified_sensitive_bytes(
                self._root,
                run_id=run_id,
                relative_path=ref.relative_path,
                expected_sha256=ref.sha256,
                max_bytes=MAX_PROTECTED_RESULT_JSON_BYTES,
            )
        except InputArtifactError as exc:
            raise self._map_input_error(exc) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
            return model.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            raise ProtectedResultStoreError("artifact failed schema validation") from exc

    @staticmethod
    def _map_input_error(
        exc: InputArtifactError, *, prefix: str = "artifact"
    ) -> ProtectedResultStoreError:
        if exc.kind is InputArtifactErrorKind.HASH_MISMATCH:
            return ProtectedResultStoreError(f"{prefix} hash mismatch")
        if exc.kind is InputArtifactErrorKind.UNSAFE_MODE:
            return ProtectedResultStoreError(f"{prefix} has unsafe permissions")
        if prefix == "source snapshot":
            return ProtectedResultStoreError("source snapshot artifact missing")
        return ProtectedResultStoreError("artifact missing or unsafe")

    @staticmethod
    def _reject_oversized(data: bytes, *, max_bytes: int) -> None:
        if len(data) > max_bytes:
            raise ProtectedResultStoreError("artifact exceeds maximum size")


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (text + "\n").encode("utf-8")


def _atomic_write_bytes_exclusive(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if hasattr(os, "chmod"):
            os.chmod(temp_path, SENSITIVE_FILE_MODE)
        try:
            os.link(temp_path, path)
        except FileExistsError:
            existing = path.read_bytes()
            if existing != data:
                raise ProtectedResultStoreError(
                    "artifact path collision with different bytes"
                ) from None
        except OSError:
            if path.exists():
                existing = path.read_bytes()
                if existing != data:
                    raise ProtectedResultStoreError(
                        "artifact path collision with different bytes"
                    ) from None
            else:
                os.replace(temp_path, path)
        if path.exists():
            set_sensitive_file_mode(path)
            _fsync_dir(path.parent)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _fsync_dir(directory: Path) -> None:
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


__all__ = [
    "MAX_FIX_PROMPT_BYTES",
    "MAX_PROTECTED_RESULT_JSON_BYTES",
    "MAX_REPLY_TEXT_BYTES",
    "MAX_SOURCE_PLAN_BYTES",
    "MAX_SOURCE_PROMPT_BYTES",
    "ProtectedResultStore",
    "ProtectedResultStoreError",
]

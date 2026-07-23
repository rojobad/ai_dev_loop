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
from ai_dev_loop.pr_review_v2.application.control_contracts import OperatorContinuationArtifact
from ai_dev_loop.pr_review_v2.application.execution_context import (
    ExecutionContextArtifact,
    ExternalAdjudicationResultArtifact,
    LocalFixResultArtifact,
    PublicationGenerationResultArtifact,
)
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    DEFAULT_MAX_PATCH_BYTES,
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
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader
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
PUBLICATION_GENERATION_DIR = "local/results/publication-generation"
ADJUDICATION_RESULT_DIR = "local/results/adjudication"
LOCAL_FIX_RESULT_DIR = "local/results/local-fix"
PUBLICATION_TEXT_DIR = "local/publication-text"
COMMIT_MESSAGE_DIR = "local/commit-message"
SOURCE_PLAN_RELATIVE = "local/source/plan.md"
SOURCE_PROMPT_RELATIVE = "local/source/prompt.txt"
# Legacy fixed paths retained for older fixtures; new writes are effect-keyed.
PUBLICATION_TEXT_RELATIVE = "local/publication-text.json"
COMMIT_MESSAGE_RELATIVE = "local/commit-message.json"
FIX_PROMPT_RELATIVE = "local/fix-prompt.txt"
FIX_PROMPTS_DIR = "local/fix-prompts"

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
    key = _adjudication_binding_key(
        run_id=run_id,
        effect_id=effect_id,
        cycle_number=cycle_number,
        bound_head_sha=bound_head_sha,
        snapshot_ref_sha256=snapshot_ref_sha256,
        execution_context_ref_sha256=execution_context_ref_sha256,
        frozen_thread_ids=frozen_thread_ids,
    )
    return f"{ADJUDICATION_RESULT_DIR}/{key}.json"


def local_fix_result_relative(
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
    return f"{LOCAL_FIX_RESULT_DIR}/{key}.json"


def publication_generation_relative(
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
    return f"{PUBLICATION_GENERATION_DIR}/{key}.json"


def publication_text_relative(
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
    return f"{PUBLICATION_TEXT_DIR}/{key}.json"


def commit_message_relative(
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
    return f"{COMMIT_MESSAGE_DIR}/{key}.json"


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
        return self._persist_deterministic_json(
            run_id=run_id,
            relative_path=publication_generation_relative(
                run_id=artifact.run_id,
                effect_id=artifact.effect_id,
                cycle_number=artifact.cycle_number,
                bound_head_sha=artifact.bound_head_sha,
                evidence_ref_sha256=artifact.evidence_ref_sha256,
                patch_ref_sha256=artifact.patch_ref_sha256,
            ),
            payload=payload,
        )

    def read_publication_generation(
        self, *, run_id: str, ref: ArtifactRef
    ) -> PublicationGenerationResultArtifact:
        return self._read_json(run_id=run_id, ref=ref, model=PublicationGenerationResultArtifact)

    def read_cached_publication_generation(
        self, effect: GeneratePublicationTextEffect
    ) -> PublicationGenerationResultArtifact | None:
        """Return effect-bound generation result only when all bindings match."""

        relative = publication_generation_relative(
            run_id=effect.run_id,
            effect_id=effect.effect_id,
            cycle_number=effect.cycle_number,
            bound_head_sha=effect.bound_head_sha,
            evidence_ref_sha256=effect.evidence_ref.sha256,
            patch_ref_sha256=effect.patch_ref.sha256,
        )
        run_root = run_artifact_root(self._root, effect.run_id)
        try:
            path = resolve_run_relative_path(run_root, relative)
        except ValueError:
            return None
        if not path.is_file() or path.is_symlink():
            return None
        try:
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            artifact = self.read_publication_generation(
                run_id=effect.run_id,
                ref=ArtifactRef(relative_path=relative, sha256=digest),
            )
        except (OSError, UnicodeError, ProtectedResultStoreError, ValueError):
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
        return self._persist_deterministic_json(
            run_id=run_id,
            relative_path=adjudication_result_relative(
                run_id=artifact.run_id,
                effect_id=artifact.effect_id,
                cycle_number=artifact.cycle_number,
                bound_head_sha=artifact.bound_head_sha,
                snapshot_ref_sha256=artifact.snapshot_ref_sha256,
                execution_context_ref_sha256=artifact.execution_context_ref_sha256,
                frozen_thread_ids=tuple(artifact.frozen_thread_ids),
            ),
            payload=payload,
        )

    def read_external_adjudication(
        self, *, run_id: str, ref: ArtifactRef
    ) -> ExternalAdjudicationResultArtifact:
        return self._read_json(run_id=run_id, ref=ref, model=ExternalAdjudicationResultArtifact)

    def read_cached_external_adjudication(
        self, effect: AdjudicateThreadsEffect
    ) -> ExternalAdjudicationResultArtifact | None:
        """Return effect-bound adjudication only when every binding matches."""

        relative = adjudication_result_relative(
            run_id=effect.run_id,
            effect_id=effect.effect_id,
            cycle_number=effect.cycle_number,
            bound_head_sha=effect.bound_head_sha,
            snapshot_ref_sha256=effect.snapshot_ref.sha256,
            execution_context_ref_sha256=effect.execution_context_ref.sha256,
            frozen_thread_ids=tuple(effect.frozen_thread_ids),
        )
        run_root = run_artifact_root(self._root, effect.run_id)
        try:
            path = resolve_run_relative_path(run_root, relative)
        except ValueError:
            return None
        if not path.is_file() or path.is_symlink():
            return None
        try:
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            artifact = self.read_external_adjudication(
                run_id=effect.run_id,
                ref=ArtifactRef(relative_path=relative, sha256=digest),
            )
        except (OSError, UnicodeError, ProtectedResultStoreError, ValueError):
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
        return self._persist_deterministic_json(
            run_id=run_id,
            relative_path=local_fix_result_relative(
                run_id=artifact.run_id,
                effect_id=artifact.effect_id,
                cycle_number=artifact.cycle_number,
                bound_head_sha=artifact.bound_head_sha,
                fix_prompt_ref_sha256=artifact.fix_prompt_ref_sha256,
                execution_context_ref_sha256=artifact.execution_context_ref_sha256,
            ),
            payload=payload,
        )

    def read_local_fix_result(self, *, run_id: str, ref: ArtifactRef) -> LocalFixResultArtifact:
        return self._read_json(run_id=run_id, ref=ref, model=LocalFixResultArtifact)

    def read_cached_local_fix_result(
        self, effect: RunLocalFixEffect
    ) -> LocalFixResultArtifact | None:
        """Return effect-bound local-fix result only when every binding matches."""

        relative = local_fix_result_relative(
            run_id=effect.run_id,
            effect_id=effect.effect_id,
            cycle_number=effect.cycle_number,
            bound_head_sha=effect.bound_head_sha,
            fix_prompt_ref_sha256=effect.fix_prompt_ref.sha256,
            execution_context_ref_sha256=effect.execution_context_ref.sha256,
        )
        run_root = run_artifact_root(self._root, effect.run_id)
        try:
            path = resolve_run_relative_path(run_root, relative)
        except ValueError:
            return None
        if not path.is_file() or path.is_symlink():
            return None
        try:
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            artifact = self.read_local_fix_result(
                run_id=effect.run_id,
                ref=ArtifactRef(relative_path=relative, sha256=digest),
            )
        except (OSError, UnicodeError, ProtectedResultStoreError, ValueError):
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
        return self._persist_bytes(run_id=run_id, relative_path=SOURCE_PLAN_RELATIVE, data=data)

    def persist_source_prompt_bytes(self, *, run_id: str, data: bytes) -> ArtifactRef:
        if not data.strip():
            raise ProtectedResultStoreError("source prompt bytes must not be empty")
        return self._persist_bytes(run_id=run_id, relative_path=SOURCE_PROMPT_RELATIVE, data=data)

    def read_source_plan_bytes(self, *, run_id: str, expected_sha256: str) -> bytes:
        return self._read_bound_bytes(
            run_id=run_id, relative_path=SOURCE_PLAN_RELATIVE, expected_sha256=expected_sha256
        )

    def read_source_prompt_bytes(self, *, run_id: str, expected_sha256: str) -> bytes:
        return self._read_bound_bytes(
            run_id=run_id, relative_path=SOURCE_PROMPT_RELATIVE, expected_sha256=expected_sha256
        )

    def _read_bound_bytes(self, *, run_id: str, relative_path: str, expected_sha256: str) -> bytes:
        run_root = run_artifact_root(self._root, run_id)
        try:
            path = resolve_run_relative_path(run_root, relative_path)
        except ValueError as exc:
            raise ProtectedResultStoreError("source snapshot artifact missing") from exc
        if not path.is_file() or path.is_symlink():
            raise ProtectedResultStoreError("source snapshot artifact missing")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_sha256:
            raise ProtectedResultStoreError("source snapshot hash mismatch")
        return raw

    def persist_operator_continuation(
        self, *, run_id: str, artifact: OperatorContinuationArtifact
    ) -> ArtifactRef:
        payload = artifact.model_dump(mode="json")
        return self._persist_json(
            run_id=run_id,
            relative_prefix=OPERATOR_CONTINUATION_DIR,
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
            pub_path = publication_text_relative(
                run_id=run_id,
                effect_id=effect_id,
                cycle_number=cycle_number,
                bound_head_sha=bound_head_sha,
                evidence_ref_sha256=evidence_ref_sha256,
                patch_ref_sha256=patch_ref_sha256,
            )
            commit_path = commit_message_relative(
                run_id=run_id,
                effect_id=effect_id,
                cycle_number=cycle_number,
                bound_head_sha=bound_head_sha,
                evidence_ref_sha256=evidence_ref_sha256,
                patch_ref_sha256=patch_ref_sha256,
            )
        else:
            pub_path = PUBLICATION_TEXT_RELATIVE
            commit_path = COMMIT_MESSAGE_RELATIVE
        pub_ref = self._persist_deterministic_json(
            run_id=run_id,
            relative_path=pub_path,
            payload=publication.model_dump(mode="json"),
        )
        commit_ref = self._persist_deterministic_json(
            run_id=run_id,
            relative_path=commit_path,
            payload=commit_message.model_dump(mode="json"),
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
        return self._persist_bytes(run_id=run_id, relative_path=relative, data=data)

    def persist_fix_prompt(self, *, run_id: str, text: str) -> ArtifactRef:
        reject_prohibited_controls(text, field_name="fix prompt")
        if not text.strip():
            raise ProtectedResultStoreError("fix prompt must not be empty")
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        relative = f"{FIX_PROMPTS_DIR}/{digest}.txt"
        return self._persist_bytes(run_id=run_id, relative_path=relative, data=data)

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
        for path in sorted(result_dir.glob("*.json")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                artifact = self.read_local_fix_result(
                    run_id=run_id,
                    ref=ArtifactRef(
                        relative_path=f"{LOCAL_FIX_RESULT_DIR}/{path.name}",
                        sha256=digest,
                    ),
                )
            except (OSError, UnicodeError, ProtectedResultStoreError, ValueError):
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
            source_path = resolve_run_relative_path(source_run_root, source_ref.relative_path)
            if not source_path.is_file() or source_path.is_symlink():
                raise ProtectedResultStoreError("source artifact missing or unsafe")
            raw = source_path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != source_ref.sha256:
                raise ProtectedResultStoreError("source artifact hash mismatch")
            dest_root = ensure_run_artifact_root(self._root, dest_run_id)
            target = resolve_run_relative_path(dest_root, dest_relative)
            ensure_dir(target.parent, mode=DIR_MODE)
            if target.exists():
                existing = target.read_bytes()
                if existing != raw:
                    raise ProtectedResultStoreError(
                        "destination artifact collision with different bytes"
                    )
                return ArtifactRef(relative_path=dest_relative, sha256=digest)
            _atomic_write_bytes_exclusive(target, raw)
            verified = hashlib.sha256(target.read_bytes()).hexdigest()
            if verified != digest:
                raise ProtectedResultStoreError("post-copy artifact hash mismatch")
            return ArtifactRef(relative_path=dest_relative, sha256=digest)
        except ProtectedResultStoreError:
            raise
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
        digest = hashlib.sha256(canonical).hexdigest()
        relative = f"{relative_prefix}/{digest}.json"
        return self._persist_canonical_bytes(run_id=run_id, relative_path=relative, data=canonical)

    def _persist_deterministic_json(
        self, *, run_id: str, relative_path: str, payload: dict[str, Any]
    ) -> ArtifactRef:
        canonical = _canonical_json_bytes(payload)
        return self._persist_canonical_bytes(
            run_id=run_id, relative_path=relative_path, data=canonical
        )

    def _persist_bytes(self, *, run_id: str, relative_path: str, data: bytes) -> ArtifactRef:
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
                existing = target.read_bytes()
                if existing != data:
                    raise ProtectedResultStoreError("artifact path collision with different bytes")
                return ArtifactRef(relative_path=relative_path, sha256=digest)
            _atomic_write_bytes_exclusive(target, data)
            verified = hashlib.sha256(target.read_bytes()).hexdigest()
            if verified != digest:
                raise ProtectedResultStoreError("post-write artifact hash mismatch")
            return ArtifactRef(relative_path=relative_path, sha256=digest)
        except ProtectedResultStoreError:
            raise
        except OSError as exc:
            raise ProtectedResultStoreError("filesystem failure while writing artifact") from exc
        except ValueError as exc:
            raise ProtectedResultStoreError("artifact path is unsafe") from exc

    def _read_json(self, *, run_id: str, ref: ArtifactRef, model: type[_TArtifact]) -> _TArtifact:
        run_root = run_artifact_root(self._root, run_id)
        try:
            path = resolve_run_relative_path(run_root, ref.relative_path)
        except ValueError as exc:
            raise ProtectedResultStoreError("artifact missing or unsafe") from exc
        if not path.is_file() or path.is_symlink():
            raise ProtectedResultStoreError("artifact missing or unsafe")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != ref.sha256:
            raise ProtectedResultStoreError("artifact hash mismatch")
        try:
            payload = json.loads(raw.decode("utf-8"))
            return model.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            raise ProtectedResultStoreError("artifact failed schema validation") from exc


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


__all__ = ["ProtectedResultStore", "ProtectedResultStoreError"]

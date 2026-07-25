"""Protected run-scoped observation artifact store for PR review v2."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, PositiveInt

from ai_dev_loop.paths import DIR_MODE, SENSITIVE_FILE_MODE, ensure_dir, set_sensitive_file_mode
from ai_dev_loop.pr_review_v2.application.contracts import AppModel
from ai_dev_loop.pr_review_v2.application.github_read import (
    MAX_TOTAL_SANITIZED_CHARS_LIMIT,
    GatewayBlockKind,
    ObservationEvidenceKind,
    ObservationSnapshot,
    ObservedIssueComment,
    ObservedReaction,
    ObservedReviewThread,
    ObservedTriggerComment,
    block_for_kind,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    ArtifactRef,
    GitSha40,
    NonEmptyId,
    NonEmptyStr,
    PullRequestBinding,
    UtcInstant,
)
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import (
    InputArtifactError,
    InputArtifactErrorKind,
    read_verified_sensitive_bytes,
)
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    ensure_run_artifact_root,
    resolve_run_relative_path,
)

OBSERVATION_SCHEMA_VERSION = 1
SANITIZATION_VERSION = 1
# Sanitized text is bounded in characters by GithubReadPolicy. UTF-8 needs at
# most four bytes per character; the fixed allowance covers bounded per-item
# metadata and canonical JSON structure without making the reader unbounded.
MAX_OBSERVATION_ARTIFACT_BYTES = 4 * MAX_TOTAL_SANITIZED_CHARS_LIMIT + 1_000_000


class ArtifactStoreError(Exception):
    """Raised when artifact persistence/verification fails (maps to EffectBlocked)."""

    def __init__(self, message: str = "artifact persistence failed") -> None:
        super().__init__(message)
        self.block = block_for_kind(GatewayBlockKind.ARTIFACT_FAILURE, detail=message)


class SanitizedThreadArtifact(AppModel):
    thread_id: NonEmptyStr
    author_login: NonEmptyStr
    created_at: UtcInstant
    commit_sha: GitSha40
    root_comment_id: NonEmptyStr
    root_body_sha256: NonEmptyStr
    path: str | None = None
    line: int | None = None
    review_id: str | None = None
    sanitized_root_body: NonEmptyStr


class SanitizedNoFindingsArtifact(AppModel):
    rule_id: NonEmptyStr
    comment_id: NonEmptyStr
    author_login: NonEmptyStr
    created_at: UtcInstant
    body_sha256: NonEmptyStr
    reviewed_commit_prefix: NonEmptyStr


class SanitizedReactionNoFindingsArtifact(AppModel):
    rule_id: Literal["accept_bot_thumbs_up"] = "accept_bot_thumbs_up"
    trigger_comment_id: NonEmptyStr
    reaction_id: NonEmptyStr
    user_login: NonEmptyStr
    content: Literal["+1"] = "+1"
    created_at: UtcInstant


class ObservationArtifactManifest(AppModel):
    schema_version: Literal[1] = 1
    sanitization_version: Literal[1] = 1
    repository: NonEmptyStr
    pr_number: PositiveInt
    head_branch: NonEmptyStr
    base_branch: NonEmptyStr
    head_sha: GitSha40
    cycle_number: PositiveInt
    poll_sequence: PositiveInt
    trigger_marker: NonEmptyId
    observed_at: UtcInstant
    evidence_kind: ObservationEvidenceKind
    trigger: ObservedTriggerComment
    eligible_threads: tuple[SanitizedThreadArtifact, ...] = ()
    no_findings: SanitizedNoFindingsArtifact | None = None
    no_findings_reaction: SanitizedReactionNoFindingsArtifact | None = None
    reaction_ids: tuple[NonEmptyStr, ...] = ()
    source_hashes: dict[str, str] = Field(default_factory=dict)


class ReviewArtifactStore:
    """Atomic protected observation artifact writer/reader."""

    def __init__(self, artifact_root: Path) -> None:
        self._root = artifact_root

    @property
    def root(self) -> Path:
        return self._root

    def persist_observation_for_run(
        self, *, run_id: str, snapshot: ObservationSnapshot
    ) -> ArtifactRef:
        try:
            run_root = ensure_run_artifact_root(self._root, run_id)
            manifest = _manifest_from_snapshot(snapshot)
            payload = manifest.model_dump(mode="json")
            canonical = _canonical_json_bytes(payload)
            if len(canonical) > MAX_OBSERVATION_ARTIFACT_BYTES:
                raise ArtifactStoreError("observation artifact exceeds maximum size")
            digest = hashlib.sha256(canonical).hexdigest()
            # Content-addressed path: competing claimants with different bytes cannot
            # overwrite an accepted ArtifactRef for the same cycle/poll.
            relative = (
                f"observations/cycle-{snapshot.cycle_number:02d}/"
                f"poll-{snapshot.poll_sequence:04d}/"
                f"{digest}.json"
            )
            target = resolve_run_relative_path(run_root, relative)
            ensure_dir(target.parent, mode=DIR_MODE)
            if target.exists():
                try:
                    existing = read_verified_sensitive_bytes(
                        self._root,
                        run_id=run_id,
                        relative_path=relative,
                        expected_sha256=digest,
                        max_bytes=MAX_OBSERVATION_ARTIFACT_BYTES,
                    )
                except InputArtifactError as exc:
                    raise _map_observation_input_error(exc) from exc
                if existing != canonical:
                    raise ArtifactStoreError(
                        "content-addressed observation path collision with different bytes"
                    )
                return ArtifactRef(relative_path=relative, sha256=digest)
            _atomic_write_bytes_exclusive(target, canonical)
            try:
                verified = read_verified_sensitive_bytes(
                    self._root,
                    run_id=run_id,
                    relative_path=relative,
                    expected_sha256=digest,
                    max_bytes=MAX_OBSERVATION_ARTIFACT_BYTES,
                )
            except InputArtifactError as exc:
                raise _map_observation_input_error(exc) from exc
            if hashlib.sha256(verified).hexdigest() != digest:
                raise ArtifactStoreError("post-write artifact hash mismatch")
            return ArtifactRef(relative_path=relative, sha256=digest)
        except ArtifactStoreError:
            raise
        except OSError as exc:
            raise ArtifactStoreError(
                "filesystem failure while writing observation artifact"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - fail closed for expected artifact faults
            if isinstance(exc, (TypeError, ValueError)):
                raise ArtifactStoreError("observation artifact serialization failed") from exc
            raise

    def read_and_verify(
        self,
        *,
        run_id: str,
        ref: ArtifactRef,
        expected_binding: PullRequestBinding | None = None,
    ) -> ObservationArtifactManifest:
        try:
            raw = read_verified_sensitive_bytes(
                self._root,
                run_id=run_id,
                relative_path=ref.relative_path,
                expected_sha256=ref.sha256,
                max_bytes=MAX_OBSERVATION_ARTIFACT_BYTES,
            )
        except InputArtifactError as exc:
            raise _map_observation_input_error(exc) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
            manifest = ObservationArtifactManifest.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            raise ArtifactStoreError("observation artifact failed schema validation") from exc
        if manifest.schema_version != OBSERVATION_SCHEMA_VERSION:
            raise ArtifactStoreError("observation artifact schema version mismatch")
        if expected_binding is not None and (
            manifest.repository != expected_binding.repository.name_with_owner
            or manifest.pr_number != expected_binding.pr_number
            or manifest.head_sha != expected_binding.head_sha
            or manifest.head_branch != expected_binding.head_branch
            or manifest.base_branch != expected_binding.base_branch
        ):
            raise ArtifactStoreError("observation artifact binding mismatch")
        return manifest


def _manifest_from_snapshot(snapshot: ObservationSnapshot) -> ObservationArtifactManifest:
    threads = tuple(
        SanitizedThreadArtifact(
            thread_id=thread.thread_id,
            author_login=thread.author_login,
            created_at=thread.created_at,
            commit_sha=thread.commit_sha,
            root_comment_id=thread.root_comment_id,
            root_body_sha256=thread.root_body_sha256,
            path=thread.path,
            line=thread.line,
            review_id=thread.review_id,
            sanitized_root_body=thread.sanitized_root_body,
        )
        for thread in snapshot.eligible_threads
    )
    no_findings = None
    if snapshot.no_findings_comment is not None:
        comment = snapshot.no_findings_comment
        if not comment.matched_no_findings_rule_id or not comment.reviewed_commit_prefix:
            raise ArtifactStoreError("no-findings artifact missing required identity fields")
        no_findings = SanitizedNoFindingsArtifact(
            rule_id=comment.matched_no_findings_rule_id,
            comment_id=comment.comment_id,
            author_login=comment.author_login,
            created_at=comment.created_at,
            body_sha256=comment.body_sha256,
            reviewed_commit_prefix=comment.reviewed_commit_prefix,
        )
    no_findings_reaction = None
    if snapshot.no_findings_reaction is not None:
        reaction = snapshot.no_findings_reaction
        no_findings_reaction = SanitizedReactionNoFindingsArtifact(
            trigger_comment_id=reaction.trigger_comment_id,
            reaction_id=reaction.reaction_id,
            user_login=reaction.user_login,
            content="+1",
            created_at=reaction.created_at,
        )
    source_hashes = {
        "trigger_body": snapshot.trigger.body_sha256,
    }
    for thread in snapshot.eligible_threads:
        source_hashes[f"thread:{thread.thread_id}"] = thread.root_body_sha256
    if snapshot.no_findings_comment is not None:
        source_hashes["no_findings_body"] = snapshot.no_findings_comment.body_sha256
    if snapshot.no_findings_reaction is not None:
        source_hashes["no_findings_reaction"] = (
            f"{snapshot.no_findings_reaction.reaction_id}:"
            f"{snapshot.no_findings_reaction.user_login}:+1"
        )
    return ObservationArtifactManifest(
        repository=snapshot.binding.repository.name_with_owner,
        pr_number=snapshot.binding.pr_number,
        head_branch=snapshot.binding.head_branch,
        base_branch=snapshot.binding.base_branch,
        head_sha=snapshot.binding.head_sha,
        cycle_number=snapshot.cycle_number,
        poll_sequence=snapshot.poll_sequence,
        trigger_marker=snapshot.trigger_marker,
        observed_at=snapshot.observed_at,
        evidence_kind=snapshot.evidence_kind,
        trigger=snapshot.trigger,
        eligible_threads=threads,
        no_findings=no_findings,
        no_findings_reaction=no_findings_reaction,
        reaction_ids=tuple(item.reaction_id for item in snapshot.reactions),
        source_hashes=source_hashes,
    )


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (text + "\n").encode("utf-8")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
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
        os.replace(temp_path, path)
        set_sensitive_file_mode(path)
        _fsync_dir(path.parent)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _atomic_write_bytes_exclusive(path: Path, data: bytes) -> None:
    """Publish bytes only when the final path does not already exist.

    Uses an exclusive create of the final path via link/rename where possible so a
    concurrent writer cannot replace an already-published content-addressed object.
    """

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
            # link fails if destination exists (collision-safe exclusive publish).
            os.link(temp_path, path)
        except FileExistsError:
            existing = path.read_bytes()
            if existing != data:
                raise ArtifactStoreError(
                    "content-addressed observation path collision with different bytes"
                ) from None
        except OSError:
            # Filesystems without link support: fall back to replace only when absent.
            if path.exists():
                existing = path.read_bytes()
                if existing != data:
                    raise ArtifactStoreError(
                        "content-addressed observation path collision with different bytes"
                    ) from None
            else:
                os.replace(temp_path, path)
        else:
            pass
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


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _map_observation_input_error(exc: InputArtifactError) -> ArtifactStoreError:
    if exc.kind is InputArtifactErrorKind.HASH_MISMATCH:
        return ArtifactStoreError("observation artifact hash mismatch")
    if exc.kind is InputArtifactErrorKind.UNSAFE_MODE:
        return ArtifactStoreError("observation artifact has unsafe permissions")
    return ArtifactStoreError("observation artifact missing or unsafe")


# Re-export observed types used by callers assembling manifests.
__all__ = [
    "ArtifactStoreError",
    "MAX_OBSERVATION_ARTIFACT_BYTES",
    "ObservationArtifactManifest",
    "ReviewArtifactStore",
    "SANITIZATION_VERSION",
    "ObservedIssueComment",
    "ObservedReaction",
    "ObservedReviewThread",
    "ObservedTriggerComment",
    "sha256_text",
]

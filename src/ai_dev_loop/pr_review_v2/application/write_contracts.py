"""Frozen write/reconciliation policies, DTOs, ports, and proof outcomes."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import Field, PositiveInt, StringConstraints, field_validator, model_validator

from ai_dev_loop.pr_review_v2.application.contracts import AppModel, EffectClaim, EffectExecutor
from ai_dev_loop.pr_review_v2.application.github_read import (
    LOCAL_RETRY_BASE_DELAYS_SECONDS,
    MAX_SERVER_DIRECTED_WAIT_SECONDS,
    AllowlistedHeaders,
    GatewayBlock,
    GatewayBlockKind,
    GatewayTransient,
    GatewayTransientKind,
    RateLimitClass,
    block_for_kind,
    compute_retry_backoff,
    map_transient_to_error_summary,
    transient_for_http_status,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    ArtifactRef,
    EffectCompletionToken,
    ErrorSummary,
    GitSha40,
    NonEmptyId,
    NonEmptyStr,
    PauseReasonKind,
    PullRequestBinding,
    ReconciliationResolutionKind,
    ReconciliationStrategyKind,
    SafeAction,
    TransientErrorKind,
    TriggerEvidence,
    UtcInstant,
    opaque_public_marker,
    validate_argv_safe_branch_name,
    validate_argv_safe_remote_ref,
)
from ai_dev_loop.pr_review_v2.domain.effects import MutatingEffect, PrReviewEffect
from ai_dev_loop.pr_review_v2.domain.events import (
    CommitRecordedOutcome,
    ConfirmedWriteOutcome,
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
    PrBoundOutcome,
    PrTextUpdatedOutcome,
    PushConfirmedOutcome,
    ReconciliationResolvedOutcome,
    ReviewTriggerConfirmedOutcome,
    ThreadReplyConfirmedOutcome,
    ThreadResolutionConfirmedOutcome,
    WriteOutcomeUncertain,
)

PUBLIC_MARKER_SCHEMA_VERSION = "v1"
CONTENT_BOUND_MARKER_SCHEMA_VERSION = "v2"
COMMIT_MESSAGE_SCHEMA_VERSION = 1
PUBLICATION_TEXT_SCHEMA_VERSION = 1
WRITE_EVIDENCE_SCHEMA_VERSION = 1

# Versioned text DTOs reject C0 controls except TAB/LF. CR is rejected so
# canonical hashes stay platform-stable.
_PROHIBITED_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

DEFAULT_REMOTE_NAME = "origin"
DEFAULT_GIT_TIMEOUT_SECONDS = 60.0
DEFAULT_GH_WRITE_TIMEOUT_SECONDS = 60.0
DEFAULT_OVERALL_WRITE_TIMEOUT_SECONDS = 180.0
DEFAULT_MAX_PATCH_BYTES = 8_000_000
DEFAULT_MAX_TEXT_BYTES = 200_000
DEFAULT_MAX_COMMIT_MESSAGE_BYTES = 64_000

_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SAFE_MARKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]*$")
_FORCE_FLAG_RE = re.compile(r"(?i)--force(?:-with-lease)?\b")

Sha256Hex = Annotated[
    str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
]


class WriteProofKind(StrEnum):
    APPLIED = "applied"
    PROVEN_NOT_APPLIED = "proven_not_applied"
    UNRESOLVED = "unresolved"


class WriteAuthorityStatus(StrEnum):
    AUTHORIZED = "authorized"
    REJECTED = "rejected"


class WritePreflightClass(StrEnum):
    TRANSIENT = "transient"
    BLOCKED = "blocked"
    ALREADY_APPLIED = "already_applied"
    AMBIGUOUS = "ambiguous"


class ContentBindingMarkerKind(StrEnum):
    PR_BODY = "pr_body"
    THREAD_REPLY = "thread_reply"
    COMMIT_TRAILER = "commit_trailer"


class GitRemoteScheme(StrEnum):
    SSH = "ssh"
    # LOCAL is only for constructor-injected local bare remotes (tests / offline
    # acceptance). It skips SSH-agent and NWO identity enforcement. Production
    # defaults to SSH.
    LOCAL = "local"


class ClaimAuthoritySnapshot(AppModel):
    """Safe identity required for a pre-mutation authority check."""

    run_id: NonEmptyId
    dispatch_id: NonEmptyId
    claim_id: NonEmptyId
    owner_id: NonEmptyId
    lease_generation: PositiveInt
    claimed_run_version: PositiveInt
    effect_id: NonEmptyId
    attempt: PositiveInt
    cycle_number: PositiveInt
    bound_head_sha: GitSha40


class ClaimAuthorityResult(AppModel):
    status: WriteAuthorityStatus
    safe_summary: NonEmptyStr | None = None


@runtime_checkable
class ClaimAuthorityGuard(Protocol):
    """Worker-supplied, engine-owned read-only authority check.

    Must not expose SQLite rows/connections and must not hold a transaction.
    """

    def check_authority(self, snapshot: ClaimAuthoritySnapshot) -> ClaimAuthorityResult: ...


@runtime_checkable
class MutatingEffectExecutor(Protocol):
    """Specialized executor that receives a live authority guard for MUTATING work."""

    def execute(
        self,
        effect: PrReviewEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
        authority: ClaimAuthorityGuard,
        claim: EffectClaim,
    ) -> EffectSucceeded | EffectRetryableFailure | EffectBlocked | WriteOutcomeUncertain: ...


class GitWritePolicy(AppModel):
    """Constructor-injected Git/SSH write policy; never reads ProjectConfig."""

    repository_cwd: NonEmptyStr
    remote_name: NonEmptyStr = DEFAULT_REMOTE_NAME
    remote_scheme: GitRemoteScheme = GitRemoteScheme.SSH
    git_command: NonEmptyStr = "git"
    ssh_command: NonEmptyStr = "ssh"
    per_call_timeout_seconds: float = Field(default=DEFAULT_GIT_TIMEOUT_SECONDS, gt=0.0, le=600.0)
    overall_timeout_seconds: float = Field(
        default=DEFAULT_OVERALL_WRITE_TIMEOUT_SECONDS, gt=0.0, le=1800.0
    )
    max_patch_bytes: PositiveInt = Field(default=DEFAULT_MAX_PATCH_BYTES, le=50_000_000)
    max_commit_message_bytes: PositiveInt = Field(
        default=DEFAULT_MAX_COMMIT_MESSAGE_BYTES, le=1_000_000
    )
    require_ssh_agent_identity: bool = True

    @field_validator("remote_name")
    @classmethod
    def validate_remote_name_field(cls, value: str) -> str:
        return validate_remote_name(value)

    @model_validator(mode="after")
    def validate_policy(self) -> GitWritePolicy:
        if self.overall_timeout_seconds < self.per_call_timeout_seconds:
            raise ValueError("overall_timeout_seconds must be >= per_call_timeout_seconds")
        if self.remote_scheme not in (GitRemoteScheme.SSH, GitRemoteScheme.LOCAL):
            raise ValueError("only SSH or LOCAL remotes are supported")
        return self

    @property
    def enforce_remote_identity(self) -> bool:
        return self.remote_scheme is GitRemoteScheme.SSH


class GitHubWritePolicy(AppModel):
    """Constructor-injected GitHub write policy; never reads ProjectConfig."""

    gh_command: NonEmptyStr = "gh"
    repository_cwd: NonEmptyStr
    per_call_timeout_seconds: float = Field(
        default=DEFAULT_GH_WRITE_TIMEOUT_SECONDS, gt=0.0, le=600.0
    )
    overall_timeout_seconds: float = Field(
        default=DEFAULT_OVERALL_WRITE_TIMEOUT_SECONDS, gt=0.0, le=1800.0
    )
    max_pages: PositiveInt = Field(default=20, le=100)
    max_items: PositiveInt = Field(default=500, le=5000)
    max_text_bytes: PositiveInt = Field(default=DEFAULT_MAX_TEXT_BYTES, le=2_000_000)
    review_command_body: NonEmptyStr = "@codex review"
    max_server_directed_wait_seconds: PositiveInt = Field(
        default=MAX_SERVER_DIRECTED_WAIT_SECONDS, le=MAX_SERVER_DIRECTED_WAIT_SECONDS
    )

    @model_validator(mode="after")
    def validate_policy(self) -> GitHubWritePolicy:
        if self.overall_timeout_seconds < self.per_call_timeout_seconds:
            raise ValueError("overall_timeout_seconds must be >= per_call_timeout_seconds")
        return self


def reject_prohibited_controls(value: str, *, field_name: str) -> str:
    if _PROHIBITED_CONTROL_RE.search(value):
        raise ValueError(f"{field_name} contains prohibited control characters")
    return value


class CommitMessageArtifact(AppModel):
    schema_name: Literal["ai_dev_loop.pr_review_v2.commit_message"] = (
        "ai_dev_loop.pr_review_v2.commit_message"
    )
    schema_version: Literal[1] = 1
    subject: NonEmptyStr
    body: str = ""

    @field_validator("subject", "body")
    @classmethod
    def reject_control(cls, value: str) -> str:
        return reject_prohibited_controls(value, field_name="commit message")


class PublicationTextArtifact(AppModel):
    schema_name: Literal["ai_dev_loop.pr_review_v2.publication_text"] = (
        "ai_dev_loop.pr_review_v2.publication_text"
    )
    schema_version: Literal[1] = 1
    title: NonEmptyStr
    body: str = ""

    @field_validator("title", "body")
    @classmethod
    def reject_control(cls, value: str) -> str:
        return reject_prohibited_controls(value, field_name="publication text")


class AdoptedExistingPrPreimageArtifact(AppModel):
    """Immutable prepare-time title/body snapshot for one-shot existing-PR adoption.

    Bound to the discovered open PR identity. Empty body is allowed (GitHub
    semantics); title must be non-empty after strip. Never print title/body in
    status, history, events, or exception messages.
    """

    schema_name: Literal["ai_dev_loop.pr_review_v2.adopted_existing_pr_preimage"] = (
        "ai_dev_loop.pr_review_v2.adopted_existing_pr_preimage"
    )
    schema_version: Literal[1] = 1
    repository: Annotated[
        str, StringConstraints(min_length=3, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    ]
    pr_number: PositiveInt
    head_branch: NonEmptyStr
    base_branch: NonEmptyStr
    head_sha: GitSha40
    title: NonEmptyStr
    body: str = ""

    @field_validator("title", "body")
    @classmethod
    def reject_control(cls, value: str) -> str:
        return reject_prohibited_controls(value, field_name="adopted existing PR preimage")

    @field_validator("head_branch", "base_branch")
    @classmethod
    def validate_branches(cls, value: str) -> str:
        return validate_argv_safe_branch_name(value)


class TriggerEvidenceArtifact(AppModel):
    schema_name: Literal["ai_dev_loop.pr_review_v2.trigger_evidence"] = (
        "ai_dev_loop.pr_review_v2.trigger_evidence"
    )
    schema_version: Literal[1] = 1
    marker: NonEmptyId
    comment_id: NonEmptyStr
    created_at: UtcInstant
    body_sha256: Sha256Hex
    head_sha: GitSha40
    pr_number: PositiveInt
    repository: NonEmptyStr


class ContentBoundMarker(AppModel):
    """Opaque self-binding public marker for PR bodies and replies."""

    operation: NonEmptyStr
    target_kind: NonEmptyStr
    idempotency_key_sha256: Sha256Hex
    content_sha256: Sha256Hex
    marker_text: NonEmptyId

    @field_validator("marker_text")
    @classmethod
    def validate_marker_text(cls, value: str) -> str:
        if not _SAFE_MARKER_RE.match(value):
            raise ValueError("marker_text contains unsafe characters")
        return value


class WriteProcessResult(AppModel):
    returncode: int
    timed_out: bool = False
    argv: tuple[str, ...] = ()
    stdout_bytes: int = 0
    stderr_bytes: int = 0


class RemoteRefObservation(AppModel):
    remote_name: NonEmptyStr
    remote_ref: NonEmptyStr
    sha: GitSha40 | None = None
    complete: bool = True


class ReconciliationProof(AppModel):
    proof: WriteProofKind
    strategy: ReconciliationStrategyKind
    confirmed_outcome: ConfirmedWriteOutcome | None = None
    next_attempt_at: UtcInstant | None = None
    safe_summary: NonEmptyStr

    @model_validator(mode="after")
    def validate_shape(self) -> ReconciliationProof:
        if self.proof is WriteProofKind.APPLIED:
            if self.confirmed_outcome is None:
                raise ValueError("applied proof requires confirmed_outcome")
            if self.next_attempt_at is not None:
                raise ValueError("applied proof forbids next_attempt_at")
        elif self.proof is WriteProofKind.PROVEN_NOT_APPLIED:
            if self.confirmed_outcome is not None:
                raise ValueError("proven_not_applied forbids confirmed_outcome")
            if self.next_attempt_at is None:
                raise ValueError("proven_not_applied requires next_attempt_at")
        else:
            if self.confirmed_outcome is not None:
                raise ValueError("unresolved forbids confirmed_outcome")
            if self.next_attempt_at is not None:
                raise ValueError("unresolved forbids next_attempt_at")
        return self


class AmbiguousWriteError(Exception):
    """Raised after a mutating process starts when application cannot be confirmed.

    The write executor converts this into ``WriteOutcomeUncertain`` for the exact
    original write; it never retries inline.
    """

    def __init__(self, safe_summary: str) -> None:
        super().__init__(safe_summary)
        self.safe_summary = safe_summary


class AuthorityLostError(Exception):
    """Raised when the pre-mutation authority guard rejects before any write.

    Zero writes were performed. The worker's ``complete_claim`` fence is the real
    authority; the executor returns a benign fenced result.
    """

    def __init__(self, safe_summary: str) -> None:
        super().__init__(safe_summary)
        self.safe_summary = safe_summary


class WriteGatewaySuccess(AppModel):
    outcome: ConfirmedWriteOutcome
    already_applied: bool = False


class WriteGatewayUncertain(AppModel):
    error: ErrorSummary
    reconciliation_identity: NonEmptyId
    original_write: MutatingEffect


def validate_branch_name(value: str) -> str:
    return validate_argv_safe_branch_name(value)


def validate_remote_name(value: str) -> str:
    """Validate a configured Git remote name (not a refspec)."""

    if not value or not _SAFE_BRANCH_RE.match(value):
        raise ValueError("remote name is unsafe or empty")
    if value.startswith("/") or value.endswith("/") or "//" in value or ".." in value:
        raise ValueError("remote name is unsafe")
    if ":" in value:
        raise ValueError("remote name must not contain ':'")
    return value


def validate_remote_ref(value: str) -> str:
    return validate_argv_safe_remote_ref(value)


def assert_no_force_argv(argv: tuple[str, ...] | list[str]) -> None:
    joined = " ".join(argv)
    if _FORCE_FLAG_RE.search(joined):
        raise ValueError("force push flags are forbidden")
    for token in argv:
        # Forbid --delete and empty-source / delete refspecs (: and :branch).
        # Ordinary sha:refs/heads/... push refspecs remain allowed.
        if token == "--delete" or token.startswith(":"):
            raise ValueError("delete refspecs are forbidden")


def sha256_hex(data: bytes | str) -> str:
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def canonicalize_publication_text(*, title: str, body: str) -> str:
    """Canonical unmarked PR text used for content-bound hashing."""

    return f"{title}\n{body}"


def derive_content_bound_marker(
    *,
    operation: str,
    target_kind: str,
    idempotency_key: str,
    canonical_content: str | bytes,
) -> ContentBoundMarker:
    """Derive a parseable public-safe marker with operation/target/key/content hashes.

    Format: ``adl-v2:{target_kind}:{operation}:{key_sha256}:{content_sha256}``.
    Hashes reveal no raw run/session/claim identity or raw body text.
    """

    if not operation or not target_kind:
        raise ValueError("operation and target_kind are required")
    if not _SAFE_MARKER_RE.match(operation) or not _SAFE_MARKER_RE.match(target_kind):
        raise ValueError("operation/target_kind contain unsafe characters")
    key_digest = sha256_hex(idempotency_key)
    content_digest = sha256_hex(canonical_content)
    marker_text = (
        f"adl-{CONTENT_BOUND_MARKER_SCHEMA_VERSION}:{target_kind}:{operation}:"
        f"{key_digest}:{content_digest}"
    )
    if not _SAFE_MARKER_RE.match(marker_text):
        raise ValueError("derived marker_text is unsafe")
    return ContentBoundMarker(
        operation=operation,
        target_kind=target_kind,
        idempotency_key_sha256=key_digest,
        content_sha256=content_digest,
        marker_text=marker_text,
    )


_OWNED_V2_MARKER_RE = re.compile(
    r"<!--\s*(adl-v2:([A-Za-z0-9._+/-]+):([A-Za-z0-9._+/-]+):([0-9a-f]{64}):([0-9a-f]{64}))\s*-->"
)


def parse_single_owned_preimage(body: str) -> ContentBoundMarker | None:
    """Parse exactly one intact v2 owned preimage marker from a body.

    Returns None when zero matches are present. Raises ValueError on duplicates or
    malformed/tampered marker placements that must never authorize an overwrite.
    """

    matches = list(_OWNED_V2_MARKER_RE.finditer(body))
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError("body contains duplicate owned markers")
    match = matches[0]
    marker_text, target_kind, operation, key_digest, content_digest = match.groups()
    return ContentBoundMarker(
        operation=operation,
        target_kind=target_kind,
        idempotency_key_sha256=key_digest,
        content_sha256=content_digest,
        marker_text=marker_text,
    )


def verify_owned_preimage_content(
    *,
    body: str,
    title: str | None,
    evidence: ContentBoundMarker,
) -> bool:
    """True when unmarked body (and optional title) match the evidence content hash."""

    try:
        unmarked_digest = unmarked_body_hash(body, evidence.marker_text)
    except ValueError:
        return False
    if evidence.target_kind == "pr_body":
        if title is None:
            return False
        unmarked = _unmarked_body(body, evidence.marker_text)
        if unmarked is None:
            return False
        return sha256_hex(canonicalize_publication_text(title=title, body=unmarked)) == (
            evidence.content_sha256
        )
    return unmarked_digest == evidence.content_sha256


def _unmarked_body(body: str, marker_text: str) -> str | None:
    needle = html_comment_marker(marker_text)
    if body.count(needle) != 1:
        return None
    if body == needle:
        return ""
    if body.endswith(f"\n{needle}"):
        return body[: -(len(needle) + 1)]
    return None


def derive_commit_trailer(*, idempotency_key: str) -> str:
    """Deterministic safe commit-message trailer derived from the effect idempotency key."""

    digest = sha256_hex(idempotency_key)
    return f"ADL-Idempotency: {digest}"


def html_comment_marker(marker_text: str) -> str:
    if not _SAFE_MARKER_RE.match(marker_text):
        raise ValueError("marker_text contains unsafe characters")
    return f"<!-- {marker_text} -->"


def append_owned_marker(body: str, marker_text: str) -> str:
    """Append exactly one owned HTML-comment marker as the final line.

    ``append_owned_marker`` and ``unmarked_body_hash`` are exact inverses: for any
    ``body`` that does not already contain the marker,
    ``unmarked_body_hash(append_owned_marker(body, m), m) == sha256_hex(body)``.
    """

    needle = html_comment_marker(marker_text)
    if needle in body:
        raise ValueError("body already contains the owned marker")
    if not body:
        return needle
    return f"{body}\n{needle}"


def unmarked_body_hash(body: str, marker_text: str) -> str:
    needle = html_comment_marker(marker_text)
    if needle not in body:
        return sha256_hex(body)
    if body.count(needle) > 1:
        raise ValueError("body contains duplicate owned markers")
    if body == needle:
        unmarked = ""
    elif body.endswith(f"\n{needle}"):
        # Exact inverse of append_owned_marker's final-line placement.
        unmarked = body[: -(len(needle) + 1)]
    else:
        # Human-edited or non-canonical placement: strip the marker only. The
        # resulting hash will not match the intended content, so reconciliation
        # conservatively classifies it as drift/unresolved.
        unmarked = body.replace(needle, "", 1)
    return sha256_hex(unmarked)


def build_reconciliation_identity(
    *,
    run_id: str,
    effect_id: str,
    attempt: int,
    strategy: ReconciliationStrategyKind,
) -> str:
    """Stable reconciliation identity independent of claim/lease generation."""

    logical = f"reconcile:{run_id}:{effect_id}:attempt:{attempt}:{strategy.value}"
    return opaque_public_marker(logical)


def claim_authority_snapshot_from_claim(claim: EffectClaim) -> ClaimAuthoritySnapshot:
    return ClaimAuthoritySnapshot(
        run_id=claim.run_id,
        dispatch_id=claim.dispatch_id,
        claim_id=claim.claim_id,
        owner_id=claim.owner_id,
        lease_generation=claim.lease_generation,
        claimed_run_version=claim.claimed_run_version,
        effect_id=claim.effect.effect_id,
        attempt=claim.attempt,
        cycle_number=claim.effect.cycle_number,
        bound_head_sha=claim.effect.bound_head_sha,
    )


def resolution_from_proof(proof: WriteProofKind) -> ReconciliationResolutionKind:
    if proof is WriteProofKind.APPLIED:
        return ReconciliationResolutionKind.APPLIED
    if proof is WriteProofKind.PROVEN_NOT_APPLIED:
        return ReconciliationResolutionKind.PROVEN_NOT_APPLIED
    return ReconciliationResolutionKind.UNRESOLVED


def opaque_run_lock_id(run_id: str) -> str:
    """Opaque lock identity derived from a run id (never the raw run id)."""

    return f"run:{sha256_hex(run_id)}"


def opaque_repository_lock_id(repository_cwd: str) -> str:
    """Opaque lock identity derived from a resolved repository path."""

    resolved = str(Path(repository_cwd).resolve())
    return f"repo:{sha256_hex(resolved)}"


def repository_lock_contention_transient() -> GatewayTransient:
    """Typed pre-write transient for nonblocking repository FileLock contention."""

    return GatewayTransient(
        kind=GatewayTransientKind.TEMPORARY_CLI_FAILURE,
        safe_summary="repository write lock is held by another local process",
        transient_kind=TransientErrorKind.TEMPORARY_CLI_FAILURE,
    )


# Re-export helpers commonly needed by gateways/executors.
__all__ = [
    "AdoptedExistingPrPreimageArtifact",
    "AllowlistedHeaders",
    "AmbiguousWriteError",
    "AuthorityLostError",
    "ClaimAuthorityGuard",
    "ClaimAuthorityResult",
    "ClaimAuthoritySnapshot",
    "CommitMessageArtifact",
    "ContentBindingMarkerKind",
    "ContentBoundMarker",
    "DEFAULT_REMOTE_NAME",
    "EffectExecutor",
    "GatewayBlock",
    "GatewayBlockKind",
    "GatewayTransient",
    "GatewayTransientKind",
    "GitHubWritePolicy",
    "GitRemoteScheme",
    "GitWritePolicy",
    "LOCAL_RETRY_BASE_DELAYS_SECONDS",
    "MutatingEffectExecutor",
    "PublicationTextArtifact",
    "RateLimitClass",
    "ReconciliationProof",
    "RemoteRefObservation",
    "Sha256Hex",
    "TriggerEvidenceArtifact",
    "WriteAuthorityStatus",
    "WriteGatewaySuccess",
    "WriteGatewayUncertain",
    "WritePreflightClass",
    "WriteProcessResult",
    "WriteProofKind",
    "append_owned_marker",
    "assert_no_force_argv",
    "block_for_kind",
    "build_reconciliation_identity",
    "canonicalize_publication_text",
    "claim_authority_snapshot_from_claim",
    "compute_retry_backoff",
    "derive_commit_trailer",
    "derive_content_bound_marker",
    "html_comment_marker",
    "parse_single_owned_preimage",
    "reject_prohibited_controls",
    "verify_owned_preimage_content",
    "map_transient_to_error_summary",
    "opaque_repository_lock_id",
    "opaque_run_lock_id",
    "repository_lock_contention_transient",
    "resolution_from_proof",
    "sha256_hex",
    "transient_for_http_status",
    "unmarked_body_hash",
    "validate_branch_name",
    "validate_remote_name",
    "validate_remote_ref",
    # Domain event types used by executors
    "CommitRecordedOutcome",
    "ConfirmedWriteOutcome",
    "EffectBlocked",
    "EffectRetryableFailure",
    "EffectSucceeded",
    "ErrorSummary",
    "PauseReasonKind",
    "PrBoundOutcome",
    "PrTextUpdatedOutcome",
    "PullRequestBinding",
    "PushConfirmedOutcome",
    "ReconciliationResolvedOutcome",
    "ReconciliationStrategyKind",
    "ReviewTriggerConfirmedOutcome",
    "SafeAction",
    "ThreadReplyConfirmedOutcome",
    "ThreadResolutionConfirmedOutcome",
    "TransientErrorKind",
    "TriggerEvidence",
    "WriteOutcomeUncertain",
    "ArtifactRef",
]

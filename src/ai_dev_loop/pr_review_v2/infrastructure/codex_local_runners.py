"""Injected Codex resume runners for PR review v2 local effects."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from ai_dev_loop.paths import DIR_MODE, ensure_dir, schema_path
from ai_dev_loop.pr_review_v2.application.execution_context import (
    ExecutionContextArtifact,
    ExternalAdjudicationDecision,
    ExternalAdjudicationResultArtifact,
    PublicationGenerationResultArtifact,
)
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    DEFAULT_MAX_COMMIT_MESSAGE_BYTES,
    DEFAULT_MAX_TEXT_BYTES,
)
from ai_dev_loop.pr_review_v2.domain.common import AdjudicationDecisionKind, ArtifactRef
from ai_dev_loop.pr_review_v2.domain.effects import (
    AdjudicateThreadsEffect,
    GeneratePublicationTextEffect,
)
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    ensure_run_artifact_root,
    resolve_run_relative_path,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
    MAX_PROTECTED_RESULT_JSON_BYTES,
)
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import (
    MAX_OBSERVATION_ARTIFACT_BYTES,
)

PUBLICATION_GENERATION_SCHEMA = "pr-review-v2-publication-generation-v1.json"
EXTERNAL_ADJUDICATION_SCHEMA = "pr-review-v2-external-adjudication-v1.json"
# Observation bytes accepted by ReviewArtifactStore must remain adjudicable.
MAX_ADJUDICATION_SNAPSHOT_BYTES = MAX_OBSERVATION_ARTIFACT_BYTES
MAX_CODEX_RESULT_FILE_BYTES = MAX_PROTECTED_RESULT_JSON_BYTES


class CodexLocalRunnerError(Exception):
    """Privacy-safe Codex local-runner failure."""

    def __init__(self, message: str = "codex local runner failed") -> None:
        super().__init__(message)


class PublicationGenerationPayload(BaseModel):
    """Strict Codex output shape matching ``pr-review-v2-publication-generation-v1.json``."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    body: str
    commit_subject: str = Field(min_length=1)
    commit_body: str


class ExternalAdjudicationDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1)
    decision: Literal["actionable", "not_applicable", "uncertain"]
    safe_summary: str = Field(min_length=1)
    reply_body: str | None = None


class ExternalAdjudicationPayload(BaseModel):
    """Strict Codex output shape for external adjudication."""

    model_config = ConfigDict(extra="forbid")

    decisions: list[ExternalAdjudicationDecisionPayload] = Field(min_length=1)
    fix_prompt_text: str | None = None


@dataclass(frozen=True)
class CodexProcessResult:
    returncode: int
    stdout_bytes: bytes
    stderr_bytes: bytes
    timed_out: bool = False


class CodexProcessRunner(Protocol):
    def run(
        self,
        argv: list[str],
        *,
        cwd: str,
        stdin_text: str,
        timeout_seconds: float,
    ) -> CodexProcessResult: ...


class FakeCodexProcessRunner:
    """Test double that writes the expected --output-last-message file before returning."""

    def __init__(
        self,
        *,
        result_payload: dict[str, object] | None = None,
        result_payloads: list[dict[str, object]] | None = None,
        returncode: int = 0,
    ) -> None:
        if result_payloads is not None:
            self._payloads = list(result_payloads)
        elif result_payload is not None:
            self._payloads = [result_payload]
        else:
            self._payloads = [{}]
        self.returncode = returncode
        self.last_argv: list[str] | None = None
        self.last_stdin: str | None = None
        self.all_argv: list[list[str]] = []

    @property
    def result_payload(self) -> dict[str, object]:
        return self._payloads[0] if self._payloads else {}

    def run(
        self,
        argv: list[str],
        *,
        cwd: str,
        stdin_text: str,
        timeout_seconds: float,
    ) -> CodexProcessResult:
        del cwd, timeout_seconds
        self.last_argv = list(argv)
        self.all_argv.append(list(argv))
        self.last_stdin = stdin_text
        payload = self._payloads.pop(0) if self._payloads else {}
        if "--output-last-message" in argv:
            index = argv.index("--output-last-message")
            result_path = Path(argv[index + 1])
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            if hasattr(os, "chmod"):
                os.chmod(result_path, stat.S_IRUSR | stat.S_IWUSR)
        return CodexProcessResult(returncode=self.returncode, stdout_bytes=b"", stderr_bytes=b"")


def build_codex_resume_argv(
    *,
    codex_command: str,
    repo_root: str,
    sandbox: str,
    session_id: str,
    review_model: str,
    review_reasoning_effort: str,
    schema_path_value: Path,
    result_path: Path,
) -> list[str]:
    return [
        codex_command,
        "exec",
        "--cd",
        repo_root,
        "--sandbox",
        sandbox,
        "resume",
        "--model",
        review_model,
        "-c",
        f'model_reasoning_effort="{review_reasoning_effort}"',
        "--json",
        "--output-schema",
        str(schema_path_value),
        "--output-last-message",
        str(result_path),
        session_id,
        "-",
    ]


class PublicationTextRunner:
    def __init__(
        self,
        *,
        artifact_root: Path,
        process_runner: CodexProcessRunner,
        timeout_seconds: float,
    ) -> None:
        self._artifact_root = artifact_root
        self._process_runner = process_runner
        self._timeout_seconds = timeout_seconds

    def generate(
        self,
        *,
        run_id: str,
        session_id: str,
        repo_root: str,
        execution_context: ExecutionContextArtifact,
        evidence_ref: ArtifactRef,
        patch_ref: ArtifactRef,
        effect: GeneratePublicationTextEffect,
    ) -> PublicationGenerationResultArtifact:
        if execution_context.codex.session_id != session_id:
            raise CodexLocalRunnerError("session identity mismatch")
        # bound_head_sha is the live effect binding (advances across cycles/commits).
        # Frozen prepare-time expected_head_sha is the origin baseline only and must
        # not reject later-cycle or fix-publication generations.
        if not effect.bound_head_sha or len(effect.bound_head_sha) != 40:
            raise CodexLocalRunnerError("head SHA binding mismatch")
        scratch = _scratch_dir(self._artifact_root, run_id, effect.effect_id, "publication")
        result_path = scratch / "result.json"
        schema_file = schema_path(PUBLICATION_GENERATION_SCHEMA)
        argv = build_codex_resume_argv(
            codex_command=execution_context.codex.command,
            repo_root=repo_root,
            sandbox=execution_context.codex.sandbox,
            session_id=session_id,
            review_model=execution_context.codex.review_model,
            review_reasoning_effort=execution_context.codex.review_reasoning_effort,
            schema_path_value=schema_file,
            result_path=result_path,
        )
        stdin_text = _publication_wrapper_prompt(
            execution_context=execution_context,
            evidence_ref=evidence_ref,
            patch_ref=patch_ref,
        )
        try:
            result = self._process_runner.run(
                argv,
                cwd=repo_root,
                stdin_text=stdin_text,
                timeout_seconds=self._timeout_seconds,
            )
            if result.timed_out:
                raise CodexLocalRunnerError("codex publication generation timed out")
            if result.returncode != 0:
                raise CodexLocalRunnerError("codex publication generation failed")
            payload = _read_bounded_result_json(result_path)
            try:
                parsed = PublicationGenerationPayload.model_validate(payload)
            except PydanticValidationError as exc:
                raise CodexLocalRunnerError(
                    "codex publication result failed schema validation"
                ) from exc
            _reject_oversized_utf8_field(parsed.title, limit=DEFAULT_MAX_TEXT_BYTES)
            _reject_oversized_utf8_field(parsed.body, limit=DEFAULT_MAX_TEXT_BYTES)
            _reject_oversized_utf8_field(
                parsed.commit_subject, limit=DEFAULT_MAX_COMMIT_MESSAGE_BYTES
            )
            _reject_oversized_utf8_field(parsed.commit_body, limit=DEFAULT_MAX_COMMIT_MESSAGE_BYTES)
            artifact = PublicationGenerationResultArtifact(
                title=parsed.title,
                body=parsed.body,
                commit_subject=parsed.commit_subject,
                commit_body=parsed.commit_body,
                run_id=effect.run_id,
                cycle_number=effect.cycle_number,
                effect_id=effect.effect_id,
                bound_head_sha=effect.bound_head_sha,
                evidence_ref_sha256=evidence_ref.sha256,
                patch_ref_sha256=patch_ref.sha256,
            )
            _reject_oversized_enriched_artifact(artifact)
            return artifact
        finally:
            _cleanup_scratch(scratch)


class ThreadAdjudicationRunner:
    def __init__(
        self,
        *,
        artifact_root: Path,
        process_runner: CodexProcessRunner,
        timeout_seconds: float,
    ) -> None:
        self._artifact_root = artifact_root
        self._process_runner = process_runner
        self._timeout_seconds = timeout_seconds

    def adjudicate(
        self,
        *,
        run_id: str,
        session_id: str,
        repo_root: str,
        execution_context: ExecutionContextArtifact,
        effect: AdjudicateThreadsEffect,
        snapshot_artifact_bytes_or_path: bytes | Path,
        frozen_thread_ids: tuple[str, ...],
    ) -> ExternalAdjudicationResultArtifact:
        if execution_context.codex.session_id != session_id:
            raise CodexLocalRunnerError("session identity mismatch")
        if set(frozen_thread_ids) != set(effect.frozen_thread_ids):
            raise CodexLocalRunnerError("frozen thread set mismatch")
        if len(frozen_thread_ids) != len(set(frozen_thread_ids)):
            raise CodexLocalRunnerError("frozen thread IDs must be unique")
        scratch = _scratch_dir(self._artifact_root, run_id, effect.effect_id, "adjudication")
        result_path = scratch / "result.json"
        schema_file = schema_path(EXTERNAL_ADJUDICATION_SCHEMA)
        argv = build_codex_resume_argv(
            codex_command=execution_context.codex.command,
            repo_root=repo_root,
            sandbox=execution_context.codex.sandbox,
            session_id=session_id,
            review_model=execution_context.codex.review_model,
            review_reasoning_effort=execution_context.codex.review_reasoning_effort,
            schema_path_value=schema_file,
            result_path=result_path,
        )
        stdin_text = _adjudication_wrapper_prompt(
            execution_context=execution_context,
            effect=effect,
            frozen_thread_ids=frozen_thread_ids,
            snapshot_artifact_bytes_or_path=snapshot_artifact_bytes_or_path,
        )
        try:
            result = self._process_runner.run(
                argv,
                cwd=repo_root,
                stdin_text=stdin_text,
                timeout_seconds=self._timeout_seconds,
            )
            if result.timed_out:
                raise CodexLocalRunnerError("codex adjudication timed out")
            if result.returncode != 0:
                raise CodexLocalRunnerError("codex adjudication failed")
            try:
                payload = _read_bounded_result_json(result_path)
                parsed = ExternalAdjudicationPayload.model_validate(payload)
                decisions = tuple(
                    ExternalAdjudicationDecision(
                        thread_id=item.thread_id,
                        decision=AdjudicationDecisionKind(item.decision),
                        safe_summary=item.safe_summary,
                        reply_body=item.reply_body,
                    )
                    for item in parsed.decisions
                )
            except (CodexLocalRunnerError, PydanticValidationError, ValueError) as exc:
                if isinstance(exc, CodexLocalRunnerError):
                    raise
                raise CodexLocalRunnerError(
                    "codex adjudication result failed schema validation"
                ) from exc
            for item in decisions:
                _reject_oversized_utf8_field(item.safe_summary, limit=DEFAULT_MAX_TEXT_BYTES)
                if item.reply_body is not None:
                    _reject_oversized_utf8_field(item.reply_body, limit=DEFAULT_MAX_TEXT_BYTES)
            if parsed.fix_prompt_text is not None:
                _reject_oversized_utf8_field(parsed.fix_prompt_text, limit=DEFAULT_MAX_TEXT_BYTES)
            artifact = ExternalAdjudicationResultArtifact(
                decisions=decisions,
                fix_prompt_text=parsed.fix_prompt_text,
                run_id=effect.run_id,
                cycle_number=effect.cycle_number,
                effect_id=effect.effect_id,
                bound_head_sha=effect.bound_head_sha,
                frozen_thread_ids=tuple(effect.frozen_thread_ids),
                snapshot_ref_sha256=effect.snapshot_ref.sha256,
                execution_context_ref_sha256=effect.execution_context_ref.sha256,
            )
            _reject_oversized_enriched_artifact(artifact)
            return artifact
        finally:
            _cleanup_scratch(scratch)


def _canonical_json_bytes(payload: dict[str, object]) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (text + "\n").encode("utf-8")


def _reject_oversized_utf8_field(value: str, *, limit: int) -> None:
    if len(value.encode("utf-8")) > limit:
        raise CodexLocalRunnerError("codex result field exceeds downstream size bound")


def _reject_oversized_enriched_artifact(
    artifact: PublicationGenerationResultArtifact | ExternalAdjudicationResultArtifact,
) -> None:
    canonical = _canonical_json_bytes(artifact.model_dump(mode="json"))
    if len(canonical) > MAX_PROTECTED_RESULT_JSON_BYTES:
        raise CodexLocalRunnerError("codex enriched result exceeds protected store size bound")


def _scratch_dir(artifact_root: Path, run_id: str, effect_id: str, kind: str) -> Path:
    run_root = ensure_run_artifact_root(artifact_root, run_id)
    safe_effect = hashlib.sha256(effect_id.encode("utf-8")).hexdigest()[:32]
    relative = f"local/scratch/{kind}/{safe_effect}"
    return ensure_dir(resolve_run_relative_path(run_root, relative), mode=DIR_MODE)


def _cleanup_scratch(scratch: Path) -> None:
    import shutil

    with contextlib.suppress(OSError):
        if scratch.is_dir() and not scratch.is_symlink():
            shutil.rmtree(scratch)


def _read_bounded_result_json(
    path: Path, *, max_bytes: int = MAX_CODEX_RESULT_FILE_BYTES
) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise CodexLocalRunnerError("codex result artifact missing")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CodexLocalRunnerError("codex result artifact unreadable") from exc
    if size > max_bytes:
        raise CodexLocalRunnerError("codex result artifact exceeds size bound")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CodexLocalRunnerError("codex result artifact unreadable") from exc
    if len(raw) > max_bytes:
        raise CodexLocalRunnerError("codex result artifact exceeds size bound")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CodexLocalRunnerError("codex result is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CodexLocalRunnerError("codex result must be a JSON object")
    return payload


def _publication_wrapper_prompt(
    *,
    execution_context: ExecutionContextArtifact,
    evidence_ref: ArtifactRef,
    patch_ref: ArtifactRef,
) -> str:
    return (
        "Generate publication text for the accepted PR review v2 patch.\n"
        f"Plan path: {execution_context.plan_prompt.plan_path}\n"
        f"Prompt path: {execution_context.plan_prompt.prompt_path}\n"
        f"Evidence ref sha256: {evidence_ref.sha256}\n"
        f"Patch ref sha256: {patch_ref.sha256}\n"
        "Return schema-constrained JSON only.\n"
    )


def _load_snapshot_bytes(snapshot_artifact_bytes_or_path: bytes | Path) -> bytes:
    if isinstance(snapshot_artifact_bytes_or_path, bytes):
        return snapshot_artifact_bytes_or_path
    try:
        return snapshot_artifact_bytes_or_path.read_bytes()
    except OSError as exc:
        raise CodexLocalRunnerError("sanitized review snapshot is unreadable") from exc


def _adjudication_wrapper_prompt(
    *,
    execution_context: ExecutionContextArtifact,
    effect: AdjudicateThreadsEffect,
    frozen_thread_ids: tuple[str, ...],
    snapshot_artifact_bytes_or_path: bytes | Path,
) -> str:
    snapshot_bytes = _load_snapshot_bytes(snapshot_artifact_bytes_or_path)
    if len(snapshot_bytes) > MAX_ADJUDICATION_SNAPSHOT_BYTES:
        raise CodexLocalRunnerError("sanitized review snapshot exceeds size bound")
    digest = hashlib.sha256(snapshot_bytes).hexdigest()
    if digest != effect.snapshot_ref.sha256:
        raise CodexLocalRunnerError("sanitized review snapshot hash drift")
    try:
        snapshot_text = snapshot_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodexLocalRunnerError("sanitized review snapshot is not UTF-8") from exc
    # Fail closed on truncated/non-JSON snapshot before delivery.
    try:
        payload = json.loads(snapshot_text)
    except json.JSONDecodeError as exc:
        raise CodexLocalRunnerError("sanitized review snapshot is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CodexLocalRunnerError("sanitized review snapshot must be an object")
    # Provenance checks against effect binding when present on the manifest.
    head = payload.get("head_sha")
    cycle = payload.get("cycle_number")
    if isinstance(head, str) and head.lower() != effect.bound_head_sha.lower():
        raise CodexLocalRunnerError("sanitized review snapshot head SHA drift")
    if isinstance(cycle, int) and cycle != effect.cycle_number:
        raise CodexLocalRunnerError("sanitized review snapshot cycle drift")
    thread_ids_in_snapshot = {
        str(item.get("thread_id"))
        for item in payload.get("eligible_threads") or ()
        if isinstance(item, dict) and item.get("thread_id")
    }
    if set(frozen_thread_ids) and not set(frozen_thread_ids).issubset(thread_ids_in_snapshot):
        raise CodexLocalRunnerError("sanitized review snapshot missing frozen threads")
    return (
        "Adjudicate frozen GitHub review threads for PR review v2.\n"
        f"External review skill: {execution_context.codex.external_review_skill}\n"
        f"Frozen thread IDs: {', '.join(frozen_thread_ids)}\n"
        f"Bound head SHA: {effect.bound_head_sha}\n"
        f"Cycle number: {effect.cycle_number}\n"
        f"Snapshot ref sha256: {effect.snapshot_ref.sha256}\n"
        "Sanitized review snapshot JSON follows. Decide only from this snapshot.\n"
        "<<<SANITIZED_REVIEW_SNAPSHOT>>>\n"
        f"{snapshot_text}"
        "<<<END_SANITIZED_REVIEW_SNAPSHOT>>>\n"
        "Return schema-constrained JSON only.\n"
    )


__all__ = [
    "MAX_ADJUDICATION_SNAPSHOT_BYTES",
    "MAX_CODEX_RESULT_FILE_BYTES",
    "CodexLocalRunnerError",
    "CodexProcessResult",
    "CodexProcessRunner",
    "ExternalAdjudicationPayload",
    "FakeCodexProcessRunner",
    "PublicationGenerationPayload",
    "PublicationTextRunner",
    "ThreadAdjudicationRunner",
    "build_codex_resume_argv",
]

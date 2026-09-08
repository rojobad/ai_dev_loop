"""Completion envelope helpers for scheduler attempt artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.infrastructure.paths import (
    resolve_run_relative_path,
    validate_sha256_hex,
)

MAX_RESULT_ENVELOPE_BYTES = 64 * 1024
MAX_RESULT_ENVELOPE_READ_BYTES = MAX_RESULT_ENVELOPE_BYTES + 1
ATTEMPT_STDOUT_REL = "attempts/{attempt_id}/stdout.txt"
ATTEMPT_STDERR_REL = "attempts/{attempt_id}/stderr.txt"
ATTEMPT_RESULT_REL = "attempts/{attempt_id}/result.json"
_VALID_TERMINATION_CLASSES = frozenset({item.value for item in TerminationClass})


@dataclass(frozen=True)
class AttemptResultEnvelope:
    attempt_id: str
    unit_identity: str
    exit_code: int
    termination_class: str
    stdout_artifact_path: str
    stdout_sha256: str
    stderr_artifact_path: str
    stderr_sha256: str


def attempt_stdout_rel(attempt_id: str) -> str:
    return ATTEMPT_STDOUT_REL.format(attempt_id=attempt_id)


def attempt_stderr_rel(attempt_id: str) -> str:
    return ATTEMPT_STDERR_REL.format(attempt_id=attempt_id)


def attempt_result_rel(attempt_id: str) -> str:
    return ATTEMPT_RESULT_REL.format(attempt_id=attempt_id)


def build_result_envelope(
    *,
    attempt_id: str,
    unit_identity: str,
    exit_code: int,
    termination_class: TerminationClass,
    stdout_artifact_path: str,
    stdout_sha256: str,
    stderr_artifact_path: str,
    stderr_sha256: str,
) -> bytes:
    payload = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "unit_identity": unit_identity,
        "exit_code": exit_code,
        "termination_class": termination_class.value,
        "stdout_artifact_path": stdout_artifact_path,
        "stdout_sha256": stdout_sha256,
        "stderr_artifact_path": stderr_artifact_path,
        "stderr_sha256": stderr_sha256,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_RESULT_ENVELOPE_BYTES:
        raise ValueError("result envelope exceeds size limit")
    return text.encode("utf-8")


def read_bounded_bytes(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(limit)


def parse_result_envelope(content: bytes) -> AttemptResultEnvelope:
    if not content or len(content) > MAX_RESULT_ENVELOPE_BYTES:
        raise ValueError("result envelope size is invalid")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("result envelope is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("result envelope must be a JSON object")
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported result envelope schema")
    required_fields = (
        "attempt_id",
        "unit_identity",
        "exit_code",
        "termination_class",
        "stdout_artifact_path",
        "stdout_sha256",
        "stderr_artifact_path",
        "stderr_sha256",
    )
    for field_name in required_fields:
        if field_name not in payload:
            raise ValueError(f"result envelope missing field {field_name}")
    attempt_id = _require_non_empty_str(payload["attempt_id"], "attempt_id")
    unit_identity = _require_non_empty_str(payload["unit_identity"], "unit_identity")
    exit_code = _require_int(payload["exit_code"], "exit_code")
    termination_class = _require_non_empty_str(payload["termination_class"], "termination_class")
    if termination_class not in _VALID_TERMINATION_CLASSES:
        raise ValueError("result envelope termination_class is invalid")
    stdout_artifact_path = _require_non_empty_str(
        payload["stdout_artifact_path"],
        "stdout_artifact_path",
    )
    stdout_sha256 = _require_non_empty_str(payload["stdout_sha256"], "stdout_sha256")
    stderr_artifact_path = _require_non_empty_str(
        payload["stderr_artifact_path"],
        "stderr_artifact_path",
    )
    stderr_sha256 = _require_non_empty_str(payload["stderr_sha256"], "stderr_sha256")
    validate_sha256_hex(stdout_sha256)
    validate_sha256_hex(stderr_sha256)
    return AttemptResultEnvelope(
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        exit_code=exit_code,
        termination_class=termination_class,
        stdout_artifact_path=stdout_artifact_path,
        stdout_sha256=stdout_sha256,
        stderr_artifact_path=stderr_artifact_path,
        stderr_sha256=stderr_sha256,
    )


def _require_non_empty_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"result envelope field {field_name} must be a non-empty string")
    return value


def _require_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"result envelope field {field_name} must be an integer")
    return value


@dataclass(frozen=True)
class ValidatedCompletionEvidence:
    envelope: AttemptResultEnvelope
    envelope_bytes: bytes
    envelope_sha256: str
    exit_code: int
    termination_class: TerminationClass


def validate_completion_evidence(
    *,
    run_root: Path,
    attempt_id: str,
    unit_identity: str,
    result_rel: str,
    stdout_rel: str,
    stderr_rel: str,
    observed_exit_code: int | None,
    observed_termination: TerminationClass | None,
) -> ValidatedCompletionEvidence:
    result_path = resolve_run_relative_path(run_root, result_rel)
    envelope_bytes = read_bounded_bytes(result_path, MAX_RESULT_ENVELOPE_READ_BYTES)
    if not envelope_bytes or len(envelope_bytes) > MAX_RESULT_ENVELOPE_BYTES:
        raise ValueError("result envelope size is invalid")
    envelope = parse_result_envelope(envelope_bytes)
    if envelope.attempt_id != attempt_id or envelope.unit_identity != unit_identity:
        raise ValueError("result envelope identity mismatch")
    if envelope.stdout_artifact_path != stdout_rel or envelope.stderr_artifact_path != stderr_rel:
        raise ValueError("result envelope artifact references mismatch")
    stdout_path = resolve_run_relative_path(run_root, envelope.stdout_artifact_path)
    stderr_path = resolve_run_relative_path(run_root, envelope.stderr_artifact_path)
    if not stdout_path.is_file() or not stderr_path.is_file():
        raise ValueError("stdout/stderr artifacts are missing")
    stdout_sha = sha256_file(stdout_path)
    stderr_sha = sha256_file(stderr_path)
    if stdout_sha != validate_sha256_hex(envelope.stdout_sha256):
        raise ValueError("stdout artifact hash mismatch")
    if stderr_sha != validate_sha256_hex(envelope.stderr_sha256):
        raise ValueError("stderr artifact hash mismatch")
    termination = TerminationClass(envelope.termination_class)
    exit_code = envelope.exit_code
    if observed_exit_code is not None and observed_exit_code != exit_code:
        raise ValueError("exit code disagrees with authoritative observation")
    if observed_termination is not None and observed_termination != termination:
        raise ValueError("termination class disagrees with authoritative observation")
    digest = envelope_sha256(envelope_bytes)
    return ValidatedCompletionEvidence(
        envelope=envelope,
        envelope_bytes=envelope_bytes,
        envelope_sha256=digest,
        exit_code=exit_code,
        termination_class=termination,
    )


def envelope_sha256(content: bytes) -> str:
    return payload_sha256(content.decode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()

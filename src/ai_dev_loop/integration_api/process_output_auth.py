"""Authenticated attempt bindings for process output resolution."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.scheduler.application.codex_evidence import (
    CodexEvidenceError,
    load_authenticated_codex_outcome,
    verify_codex_invocation_evidence,
)
from ai_dev_loop.scheduler.application.cursor_evidence import (
    CursorEvidenceError,
    load_authenticated_cursor_outcome,
    verify_cursor_invocation_evidence,
)
from ai_dev_loop.scheduler.domain.codex_contract import CODEX_ATTEMPT_EFFECT_KINDS
from ai_dev_loop.scheduler.domain.cursor_contract import (
    CREATE_CHAT_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
    invocation_evidence_rel,
)
from ai_dev_loop.scheduler.infrastructure.paths import resolve_run_relative_path


class ProcessOutputIntegrityError(Exception):
    """Trusted process-output evidence failed verification."""


@dataclass(frozen=True)
class AuthenticatedProcessAttempt:
    invocation: dict[str, object]
    outcome: dict[str, object] | None
    writer_stopped: bool


def writer_stopped_for_status(status: str) -> bool:
    return status in {"completed", "failed"}


def attempt_reconciliation_ingested(attempt_row: sqlite3.Row) -> bool:
    keys = attempt_row.keys()
    if "ingested" not in keys or attempt_row["ingested"] is None:
        return False
    return int(attempt_row["ingested"]) == 1


def execution_writer_stopped(
    status: str,
    *,
    ingested: bool,
) -> bool:
    if writer_stopped_for_status(status):
        return True
    return status == "cancelled" and ingested


def assert_confined_artifact_path(run_root: Path, relative_path: str) -> Path:
    try:
        path = resolve_run_relative_path(run_root, relative_path)
    except ValueError as exc:
        raise ProcessOutputIntegrityError("artifact path is not safe to read") from exc
    if path.is_symlink():
        raise ProcessOutputIntegrityError("artifact path is not safe to read")
    return path


def _require_launch_binding(attempt_row: sqlite3.Row) -> tuple[str, str, str]:
    unit_identity = str(attempt_row["unit_identity"] or "").strip()
    launch_intent_sha256 = str(attempt_row["launch_intent_sha256"] or "").strip()
    launch_nonce = str(attempt_row["launch_nonce"] or "").strip()
    if not unit_identity or not launch_intent_sha256 or not launch_nonce:
        raise ProcessOutputIntegrityError("attempt launch binding incomplete")
    return unit_identity, launch_intent_sha256, launch_nonce


def authenticate_process_attempt(
    run_root: Path,
    attempt_row: sqlite3.Row,
    *,
    effect_kind: str,
) -> AuthenticatedProcessAttempt:
    attempt_id = str(attempt_row["attempt_id"])
    run_id = str(attempt_row["run_id"])
    dispatch_id = str(attempt_row["dispatch_id"])
    status = str(attempt_row["status"])
    unit_identity, launch_intent_sha256, launch_nonce = _require_launch_binding(attempt_row)

    try:
        assert_confined_artifact_path(run_root, invocation_evidence_rel(attempt_id))
        if effect_kind in CODEX_ATTEMPT_EFFECT_KINDS:
            invocation = verify_codex_invocation_evidence(
                run_root,
                attempt_id=attempt_id,
                run_id=run_id,
                dispatch_id=dispatch_id,
                unit_identity=unit_identity,
                launch_nonce=launch_nonce,
                launch_intent_sha256=launch_intent_sha256,
                effect_kind=effect_kind,
            )
        elif effect_kind in {RUN_CURSOR_TURN_EFFECT_KIND, CREATE_CHAT_EFFECT_KIND}:
            invocation = verify_cursor_invocation_evidence(
                run_root,
                attempt_id=attempt_id,
                run_id=run_id,
                dispatch_id=dispatch_id,
                unit_identity=unit_identity,
                launch_nonce=launch_nonce,
                launch_intent_sha256=launch_intent_sha256,
                effect_kind=effect_kind,
            )
        else:
            return AuthenticatedProcessAttempt(
                invocation={},
                outcome=None,
                writer_stopped=writer_stopped_for_status(status),
            )
    except (CodexEvidenceError, CursorEvidenceError) as exc:
        raise ProcessOutputIntegrityError(str(exc)) from exc

    if str(invocation.get("effect_kind", "")) != effect_kind:
        raise ProcessOutputIntegrityError("invocation effect_kind disagrees with dispatch")

    outcome: dict[str, object] | None = None
    completion_sha = attempt_row["completion_envelope_sha256"]
    completion_envelope_sha256 = str(completion_sha).strip() if completion_sha is not None else None

    if status in {"launching", "active"}:
        outcome = None
    elif not completion_envelope_sha256:
        if status in {"uncertain", "cancelled"}:
            outcome = None
        elif status in {"completed", "failed"}:
            raise ProcessOutputIntegrityError("completion binding missing from ledger")
        else:
            outcome = None
    else:
        stdout_rel = str(attempt_row["stdout_artifact_path"])
        stderr_rel = str(attempt_row["stderr_artifact_path"])
        result_rel = str(attempt_row["result_artifact_path"])
        row_keys = attempt_row.keys()
        exit_code: int | None = None
        if "exit_code" in row_keys and attempt_row["exit_code"] is not None:
            exit_code = int(attempt_row["exit_code"])
        try:
            if effect_kind in CODEX_ATTEMPT_EFFECT_KINDS:
                outcome = load_authenticated_codex_outcome(
                    run_root,
                    attempt_id=attempt_id,
                    unit_identity=unit_identity,
                    result_rel=result_rel,
                    stdout_rel=stdout_rel,
                    stderr_rel=stderr_rel,
                    observed_exit_code=exit_code,
                    expected_envelope_sha256=completion_envelope_sha256,
                    expected_dispatch_id=dispatch_id,
                    expected_effect_kind=effect_kind,
                )
            elif effect_kind in {RUN_CURSOR_TURN_EFFECT_KIND, CREATE_CHAT_EFFECT_KIND}:
                outcome = load_authenticated_cursor_outcome(
                    run_root,
                    attempt_id=attempt_id,
                    unit_identity=unit_identity,
                    result_rel=result_rel,
                    stdout_rel=stdout_rel,
                    stderr_rel=stderr_rel,
                    observed_exit_code=exit_code,
                    expected_envelope_sha256=completion_envelope_sha256,
                    expected_dispatch_id=dispatch_id,
                    expected_effect_kind=effect_kind,
                )
        except (CodexEvidenceError, CursorEvidenceError) as exc:
            raise ProcessOutputIntegrityError(str(exc)) from exc
        except ValueError as exc:
            raise ProcessOutputIntegrityError("completion envelope validation failed") from exc

    ingested = attempt_reconciliation_ingested(attempt_row)
    return AuthenticatedProcessAttempt(
        invocation=invocation,
        outcome=outcome,
        writer_stopped=execution_writer_stopped(status, ingested=ingested),
    )

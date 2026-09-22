"""Independent expected bytes for Phase 21.6 public Bridge acceptance (not from API responses)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from tests.conftest import FIXTURE_REPO
from tests.integration.phase21_4_helpers import (
    BOOTSTRAP_ID,
    FIX_PROMPT_ALPHA,
    FIX_PROMPT_BETA,
    MARKDOWN_ALPHA,
    MARKDOWN_BETA_PREFIX,
)

from ai_dev_loop.iterations import CORRECTION_ENVELOPE_HEADER

BRIDGE_CHILD_STDOUT_SENTINEL = b"BRIDGE-C04-CODEX-STDOUT-SENTINEL"
BRIDGE_CHILD_STDERR_SENTINEL = b"BRIDGE-C04-CODEX-STDERR-SENTINEL"

PLAN_BYTES = (FIXTURE_REPO / "docs/plans/sample-plan.md").read_bytes()
INITIAL_PROMPT_BYTES = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_bytes()

INVALID_REVIEW_RESPONSE_BYTES = b"not-json"
NO_FINDINGS_MARKDOWN_BYTES = b"# Review\n\nNo issues found."

REPOSITORY_PLAN_PATH_BYTES = b"docs/plans/sample-plan.md"
REVIEW_SKILL_INVOCATION_BYTES = (
    b"Invoke the configured review skill exactly as: $review-staged-cursor-execution"
)
PROMPT_MARKER_ORIGINAL_CURSOR = b"## Original Cursor prompt\n\n"
PROMPT_MARKER_LATEST_CURSOR = b"\n\n## Latest Cursor final response\n\n"
PROMPT_MARKER_ARTIFACT_PATHS = b"\n\n## Artifact paths for auditability\n"
REVIEW_PROMPT_FOOTER_BYTES = (
    b"Your final response must conform to codex-review-result-v1.json.\n"
    b"When actionable findings exist, return a complete English cursor_fix_prompt.\n"
    b"When there are no actionable findings, return cursor_fix_prompt: null.\n"
)

SEQUENCE_COMPLETION_REPORT_SCHEMA_VERSION = 1
EXPECTED_SEQUENCE_MANIFEST_NAME = "fixture-sequence"

MARKDOWN_ALPHA_BYTES = MARKDOWN_ALPHA.encode("utf-8")
FIX_PROMPT_ALPHA_BYTES = FIX_PROMPT_ALPHA.encode("utf-8")
FIX_PROMPT_BETA_BYTES = FIX_PROMPT_BETA.encode("utf-8")

_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
_GIT_OBJECT_SHA = re.compile(r"^[a-f0-9]{40}$")


def expected_cursor_final_response_bytes(agent_prompt_bytes: bytes) -> bytes:
    """Mirror fake `agent` stream-json result: done: <first 32 chars of positional prompt>."""

    prompt_text = agent_prompt_bytes.decode("utf-8")
    return f"done: {prompt_text[:32]}".encode()


CORRECTION_CURSOR_PROMPT_PREFIX_BYTES = CORRECTION_ENVELOPE_HEADER.encode("utf-8")


def expected_latest_cursor_final_response_for_cursor_iteration(cursor_iteration: int) -> bytes:
    if cursor_iteration <= 1:
        return expected_cursor_final_response_bytes(INITIAL_PROMPT_BYTES)
    if cursor_iteration == 2:
        return expected_cursor_final_response_bytes(
            CORRECTION_CURSOR_PROMPT_PREFIX_BYTES + FIX_PROMPT_ALPHA_BYTES
        )
    if cursor_iteration == 3:
        return expected_cursor_final_response_bytes(
            CORRECTION_CURSOR_PROMPT_PREFIX_BYTES + FIX_PROMPT_BETA_BYTES
        )
    raise AssertionError(
        f"unsupported cursor iteration for latest Cursor response: {cursor_iteration}"
    )


EXPECTED_LATEST_CURSOR_FINAL_RESPONSE_ITERATION_01 = (
    expected_latest_cursor_final_response_for_cursor_iteration(1)
)
EXPECTED_LATEST_CURSOR_FINAL_RESPONSE_ITERATION_02 = (
    expected_latest_cursor_final_response_for_cursor_iteration(2)
)
EXPECTED_LATEST_CURSOR_FINAL_RESPONSE_ITERATION_03 = (
    expected_latest_cursor_final_response_for_cursor_iteration(3)
)


def expected_latest_cursor_final_response_for_review_iteration(iteration: int) -> bytes:
    return expected_latest_cursor_final_response_for_cursor_iteration(iteration)


@dataclass(frozen=True)
class IndependentSequencePhaseExpectation:
    ordinal: int
    phase_name: str
    accepted_outcome: Literal["completed", "completed_with_residual_risk"]
    residual_risk: bool
    attempt_count: int
    attempt_kind_labels: tuple[str, ...]
    requires_checkpoint_evidence: bool


C04_FIXTURE_SEQUENCE_PHASE_EXPECTATIONS: tuple[IndependentSequencePhaseExpectation, ...] = (
    IndependentSequencePhaseExpectation(
        ordinal=1,
        phase_name="phase-one",
        accepted_outcome="completed",
        residual_risk=False,
        attempt_count=2,
        attempt_kind_labels=("planned_run", "same_reviewer_retry"),
        requires_checkpoint_evidence=True,
    ),
    IndependentSequencePhaseExpectation(
        ordinal=2,
        phase_name="phase-two",
        accepted_outcome="completed",
        residual_risk=False,
        attempt_count=1,
        attempt_kind_labels=("planned_run",),
        requires_checkpoint_evidence=False,
    ),
)


@dataclass(frozen=True)
class IndependentSequenceReportExpectation:
    sequence_id: str
    sequence_name: str
    final_outcome: Literal["completed", "completed_with_residual_risk"]
    final_run_id: str
    run_ids_by_ordinal: dict[int, str]
    phase_expectations: tuple[IndependentSequencePhaseExpectation, ...] = (
        C04_FIXTURE_SEQUENCE_PHASE_EXPECTATIONS
    )
    residual_risk_ordinals: tuple[int, ...] = ()
    residual_risk_phase_names: tuple[str, ...] = ()


LARGE_MARKDOWN_PAD_BYTES = 90_000
MARKDOWN_BETA_LARGE_BYTES = (MARKDOWN_BETA_PREFIX + "x" * LARGE_MARKDOWN_PAD_BYTES).encode("utf-8")

_NO_FINDINGS_RESULT = {
    "has_actionable_findings": False,
    "findings_count": 0,
    "highest_severity": None,
    "review_markdown": NO_FINDINGS_MARKDOWN_BYTES.decode("utf-8"),
    "cursor_fix_prompt": None,
    "tests_status": "passed",
    "summary": "No actionable findings.",
}


def compact_review_json(result: dict[str, object]) -> bytes:
    return json.dumps(result).encode("utf-8")


NO_FINDINGS_REVIEW_JSON = compact_review_json(_NO_FINDINGS_RESULT)


def findings_review_json(*, markdown: str, fix_prompt: str) -> bytes:
    return compact_review_json(
        {
            "has_actionable_findings": True,
            "findings_count": 1,
            "highest_severity": "P1",
            "review_markdown": markdown,
            "cursor_fix_prompt": fix_prompt,
            "tests_status": "skipped_findings_present",
            "summary": "One actionable finding.",
        }
    )


FINDINGS_ALPHA_REVIEW_JSON = findings_review_json(
    markdown=MARKDOWN_ALPHA,
    fix_prompt=FIX_PROMPT_ALPHA,
)
FINDINGS_BETA_LARGE_REVIEW_JSON = findings_review_json(
    markdown=MARKDOWN_BETA_PREFIX + "x" * LARGE_MARKDOWN_PAD_BYTES,
    fix_prompt=FIX_PROMPT_BETA,
)


def _thread_started_line() -> bytes:
    return json.dumps({"type": "thread.started", "thread_id": BOOTSTRAP_ID}).encode("utf-8") + b"\n"


CODEX_INVALID_REVIEW_STDOUT = (
    BRIDGE_CHILD_STDOUT_SENTINEL
    + b"\n"
    + json.dumps({"type": "message", "content": "invalid"}).encode("utf-8")
    + b"\n"
)
CODEX_INVALID_REVIEW_STDOUT_WITH_THREAD = (
    BRIDGE_CHILD_STDOUT_SENTINEL
    + b"\n"
    + _thread_started_line()
    + json.dumps({"type": "message", "content": "invalid"}).encode("utf-8")
    + b"\n"
)
CODEX_SUCCESS_REVIEW_STDOUT = (
    BRIDGE_CHILD_STDOUT_SENTINEL
    + b"\n"
    + json.dumps({"type": "message", "content": "review complete"}).encode("utf-8")
    + b"\n"
)
CODEX_SUCCESS_REVIEW_STDOUT_WITH_THREAD = (
    BRIDGE_CHILD_STDOUT_SENTINEL
    + b"\n"
    + _thread_started_line()
    + json.dumps({"type": "message", "content": "review complete"}).encode("utf-8")
    + b"\n"
)
CODEX_SUCCESS_REVIEW_STDERR = BRIDGE_CHILD_STDERR_SENTINEL + b"\n"

_USAGE_LIMIT_EVENT_TAIL = (
    json.dumps({"type": "error", "message": "usage_limit_exceeded"}).encode("utf-8")
    + b"\n"
    + json.dumps({"type": "turn.failed", "error": {"message": "usage_limit_exceeded"}}).encode(
        "utf-8"
    )
    + b"\n"
)
USAGE_LIMIT_REVIEW_STDOUT = BRIDGE_CHILD_STDOUT_SENTINEL + b"\n" + _USAGE_LIMIT_EVENT_TAIL
USAGE_LIMIT_REVIEW_STDOUT_WITH_THREAD = (
    BRIDGE_CHILD_STDOUT_SENTINEL + b"\n" + _thread_started_line() + _USAGE_LIMIT_EVENT_TAIL
)
USAGE_LIMIT_REVIEW_STDERR = BRIDGE_CHILD_STDERR_SENTINEL + b"\ncodex usage limit exceeded\n"

MULTI_CHUNK_PAD = b"x" * 5000


REVIEW_PROMPT_NORMAL_OPENING_BYTES = b"This is an automated Codex review turn"
REVIEW_RETRY_ENVELOPE_PREFIX_BYTES = (
    b"This is an automated scheduler retry of Codex review. "
    b"The prior attempt did not yield a valid structured review result. "
    b"Return a fresh schema-valid review of the same staged snapshot only.\n\n"
)


def expected_latest_cursor_final_response_for_captured_prompt(
    prompt_bytes: bytes,
    *,
    review_iteration: int,
) -> bytes:
    del prompt_bytes
    return expected_latest_cursor_final_response_for_cursor_iteration(review_iteration)


def verify_captured_review_prompt_bytes(prompt_bytes: bytes, *, iteration: int) -> None:
    """Verify review stdin prompt bytes using fixture-independent markers and embeds."""

    body = prompt_bytes
    if prompt_bytes.startswith(REVIEW_RETRY_ENVELOPE_PREFIX_BYTES):
        body = prompt_bytes[len(REVIEW_RETRY_ENVELOPE_PREFIX_BYTES) :]
    if not body.startswith(REVIEW_PROMPT_NORMAL_OPENING_BYTES):
        raise AssertionError("review prompt missing automated review opening")
    if REVIEW_SKILL_INVOCATION_BYTES not in prompt_bytes:
        raise AssertionError("review prompt missing configured review skill invocation")
    repo_marker = b"- Repository plan path: " + REPOSITORY_PLAN_PATH_BYTES + b"\n"
    repo_at = prompt_bytes.find(repo_marker)
    if repo_at == -1:
        raise AssertionError("review prompt missing repository plan path marker")
    original_at = prompt_bytes.find(PROMPT_MARKER_ORIGINAL_CURSOR, repo_at)
    if original_at == -1:
        raise AssertionError("review prompt missing original Cursor prompt section")
    cursor_start = original_at + len(PROMPT_MARKER_ORIGINAL_CURSOR)
    latest_at = prompt_bytes.find(PROMPT_MARKER_LATEST_CURSOR, cursor_start)
    if latest_at == -1:
        raise AssertionError("review prompt missing latest Cursor response section")
    embedded = prompt_bytes[cursor_start:latest_at]
    if embedded != INITIAL_PROMPT_BYTES:
        raise AssertionError(
            f"review prompt embedded Cursor prompt byte mismatch: "
            f"expected {len(INITIAL_PROMPT_BYTES)} bytes, got {len(embedded)}"
        )
    latest_start = latest_at + len(PROMPT_MARKER_LATEST_CURSOR)
    artifact_at = prompt_bytes.find(PROMPT_MARKER_ARTIFACT_PATHS, latest_start)
    if artifact_at == -1:
        raise AssertionError("review prompt missing artifact paths section")
    if not prompt_bytes[artifact_at:].startswith(PROMPT_MARKER_ARTIFACT_PATHS):
        raise AssertionError("review prompt artifact paths marker misaligned")
    latest_body = prompt_bytes[latest_start:artifact_at]
    expected_latest = expected_latest_cursor_final_response_for_captured_prompt(
        prompt_bytes,
        review_iteration=iteration,
    )
    if latest_body != expected_latest:
        raise AssertionError(
            "review prompt latest Cursor final response byte mismatch: "
            f"expected {expected_latest!r}, got {latest_body!r}"
        )
    if not prompt_bytes.endswith(REVIEW_PROMPT_FOOTER_BYTES):
        raise AssertionError("review prompt missing expected footer contract")
    iter_label = f"{iteration:02d}".encode("ascii")
    required_paths = (
        b"cursor/iterations/" + iter_label + b"/events.jsonl",
        b"cursor/iterations/" + iter_label + b"/final.txt",
        b"git/diffs/" + iter_label + b".patch",
    )
    for path in required_paths:
        if path not in prompt_bytes:
            raise AssertionError(f"review prompt missing artifact path {path!r}")


def assert_independent_sequence_report_content(
    report_bytes: bytes,
    *,
    expectation: IndependentSequenceReportExpectation,
) -> None:
    payload = json.loads(report_bytes.decode("utf-8"))
    if payload.get("schema_version") != SEQUENCE_COMPLETION_REPORT_SCHEMA_VERSION:
        raise AssertionError(
            f"sequence report schema_version expected "
            f"{SEQUENCE_COMPLETION_REPORT_SCHEMA_VERSION}, got {payload.get('schema_version')!r}"
        )
    if payload.get("sequence_id") != expectation.sequence_id:
        raise AssertionError(
            f"sequence report sequence_id expected {expectation.sequence_id!r}, "
            f"got {payload.get('sequence_id')!r}"
        )
    if payload.get("sequence_name") != expectation.sequence_name:
        raise AssertionError(
            f"sequence report sequence_name expected {expectation.sequence_name!r}, "
            f"got {payload.get('sequence_name')!r}"
        )
    if payload.get("final_outcome") != expectation.final_outcome:
        raise AssertionError(
            f"sequence report final_outcome expected {expectation.final_outcome!r}, "
            f"got {payload.get('final_outcome')!r}"
        )
    if payload.get("final_run_id") != expectation.final_run_id:
        raise AssertionError(
            f"sequence report final_run_id expected {expectation.final_run_id!r}, "
            f"got {payload.get('final_run_id')!r}"
        )
    final_prefix = payload.get("final_run_id_prefix")
    if final_prefix != expectation.final_run_id[:8]:
        raise AssertionError(
            f"sequence report final_run_id_prefix expected "
            f"{expectation.final_run_id[:8]!r}, got {final_prefix!r}"
        )
    if tuple(payload.get("residual_risk_ordinals") or ()) != expectation.residual_risk_ordinals:
        raise AssertionError("sequence report residual_risk_ordinals mismatch")
    if (
        tuple(payload.get("residual_risk_phase_names") or ())
        != expectation.residual_risk_phase_names
    ):
        raise AssertionError("sequence report residual_risk_phase_names mismatch")

    phases = payload.get("phases")
    if not isinstance(phases, list):
        raise AssertionError(f"sequence report phases must be a list, got {type(phases)!r}")
    if len(phases) != len(expectation.phase_expectations):
        raise AssertionError(
            f"sequence report expected {len(expectation.phase_expectations)} phases, "
            f"got {len(phases)}"
        )

    by_ordinal = {int(item["ordinal"]): item for item in phases if isinstance(item, dict)}
    for phase_spec in expectation.phase_expectations:
        entry = by_ordinal.get(phase_spec.ordinal)
        if entry is None:
            raise AssertionError(f"sequence report missing phase ordinal {phase_spec.ordinal}")
        expected_run_id = expectation.run_ids_by_ordinal.get(phase_spec.ordinal)
        if expected_run_id is None:
            raise AssertionError(
                f"missing controlled run_id for sequence phase ordinal {phase_spec.ordinal}"
            )
        if entry.get("phase_name") != phase_spec.phase_name:
            raise AssertionError(
                f"phase {phase_spec.ordinal} name expected {phase_spec.phase_name!r}, "
                f"got {entry.get('phase_name')!r}"
            )
        if entry.get("run_id") != expected_run_id:
            raise AssertionError(
                f"phase {phase_spec.ordinal} run_id expected {expected_run_id!r}, "
                f"got {entry.get('run_id')!r}"
            )
        if entry.get("run_id_prefix") != expected_run_id[:8]:
            raise AssertionError(f"phase {phase_spec.ordinal} run_id_prefix mismatch")
        if entry.get("accepted_outcome") != phase_spec.accepted_outcome:
            raise AssertionError(
                f"phase {phase_spec.ordinal} accepted_outcome expected "
                f"{phase_spec.accepted_outcome!r}, got {entry.get('accepted_outcome')!r}"
            )
        if bool(entry.get("residual_risk")) != phase_spec.residual_risk:
            raise AssertionError(f"phase {phase_spec.ordinal} residual_risk mismatch")
        if int(entry.get("attempt_count", 0)) != phase_spec.attempt_count:
            raise AssertionError(
                f"phase {phase_spec.ordinal} attempt_count expected "
                f"{phase_spec.attempt_count}, got {entry.get('attempt_count')!r}"
            )
        labels = entry.get("attempt_kind_labels")
        if tuple(labels or ()) != phase_spec.attempt_kind_labels:
            raise AssertionError(
                f"phase {phase_spec.ordinal} attempt_kind_labels expected "
                f"{phase_spec.attempt_kind_labels!r}, got {labels!r}"
            )
        review_sha = entry.get("review_result_sha256")
        if not isinstance(review_sha, str) or not _SHA256_HEX.fullmatch(review_sha):
            raise AssertionError(f"phase {phase_spec.ordinal} review_result_sha256 invalid")
        review_prefix = entry.get("review_result_sha256_prefix")
        if review_prefix != review_sha[:12]:
            raise AssertionError(f"phase {phase_spec.ordinal} review_result_sha256_prefix mismatch")
        accepted_prefix = entry.get("accepted_run_id_prefix")
        if accepted_prefix != expected_run_id[:8]:
            raise AssertionError(f"phase {phase_spec.ordinal} accepted_run_id_prefix mismatch")
        if phase_spec.requires_checkpoint_evidence:
            commit = entry.get("checkpoint_commit_sha256")
            if not isinstance(commit, str) or not _GIT_OBJECT_SHA.fullmatch(commit):
                raise AssertionError(f"phase {phase_spec.ordinal} missing checkpoint_commit_sha256")
            if entry.get("checkpoint_commit_sha256_prefix") != commit[:12]:
                raise AssertionError(
                    f"phase {phase_spec.ordinal} checkpoint_commit_sha256_prefix mismatch"
                )
            parent = entry.get("checkpoint_parent_sha256")
            if not isinstance(parent, str) or not _GIT_OBJECT_SHA.fullmatch(parent):
                raise AssertionError(f"phase {phase_spec.ordinal} missing checkpoint_parent_sha256")
        else:
            if entry.get("checkpoint_commit_sha256") is not None:
                raise AssertionError(
                    f"phase {phase_spec.ordinal} must not carry checkpoint_commit_sha256"
                )

    final_patch = payload.get("final_staged_patch_sha256")
    if not isinstance(final_patch, str) or not _SHA256_HEX.fullmatch(final_patch):
        raise AssertionError("sequence report final_staged_patch_sha256 invalid")
    if payload.get("final_staged_patch_sha256_prefix") != final_patch[:12]:
        raise AssertionError("sequence report final_staged_patch_sha256_prefix mismatch")
    base_head = payload.get("base_head_sha256")
    if not isinstance(base_head, str) or not _GIT_OBJECT_SHA.fullmatch(base_head):
        raise AssertionError("sequence report base_head_sha256 invalid")
    if payload.get("base_head_sha256_prefix") != base_head[:12]:
        raise AssertionError("sequence report base_head_sha256_prefix mismatch")


__all__ = [
    "BRIDGE_CHILD_STDERR_SENTINEL",
    "BRIDGE_CHILD_STDOUT_SENTINEL",
    "C04_FIXTURE_SEQUENCE_PHASE_EXPECTATIONS",
    "CODEX_INVALID_REVIEW_STDOUT",
    "CODEX_INVALID_REVIEW_STDOUT_WITH_THREAD",
    "CODEX_SUCCESS_REVIEW_STDERR",
    "CODEX_SUCCESS_REVIEW_STDOUT",
    "CODEX_SUCCESS_REVIEW_STDOUT_WITH_THREAD",
    "EXPECTED_LATEST_CURSOR_FINAL_RESPONSE_ITERATION_01",
    "EXPECTED_LATEST_CURSOR_FINAL_RESPONSE_ITERATION_02",
    "EXPECTED_LATEST_CURSOR_FINAL_RESPONSE_ITERATION_03",
    "EXPECTED_SEQUENCE_MANIFEST_NAME",
    "FINDINGS_ALPHA_REVIEW_JSON",
    "FINDINGS_BETA_LARGE_REVIEW_JSON",
    "FIX_PROMPT_ALPHA_BYTES",
    "FIX_PROMPT_BETA_BYTES",
    "INITIAL_PROMPT_BYTES",
    "INVALID_REVIEW_RESPONSE_BYTES",
    "IndependentSequencePhaseExpectation",
    "IndependentSequenceReportExpectation",
    "LARGE_MARKDOWN_PAD_BYTES",
    "MARKDOWN_ALPHA_BYTES",
    "MARKDOWN_BETA_LARGE_BYTES",
    "NO_FINDINGS_MARKDOWN_BYTES",
    "NO_FINDINGS_REVIEW_JSON",
    "PLAN_BYTES",
    "PROMPT_MARKER_ARTIFACT_PATHS",
    "PROMPT_MARKER_LATEST_CURSOR",
    "PROMPT_MARKER_ORIGINAL_CURSOR",
    "REPOSITORY_PLAN_PATH_BYTES",
    "REVIEW_PROMPT_FOOTER_BYTES",
    "REVIEW_PROMPT_NORMAL_OPENING_BYTES",
    "REVIEW_RETRY_ENVELOPE_PREFIX_BYTES",
    "REVIEW_SKILL_INVOCATION_BYTES",
    "SEQUENCE_COMPLETION_REPORT_SCHEMA_VERSION",
    "USAGE_LIMIT_REVIEW_STDERR",
    "USAGE_LIMIT_REVIEW_STDOUT",
    "USAGE_LIMIT_REVIEW_STDOUT_WITH_THREAD",
    "assert_independent_sequence_report_content",
    "compact_review_json",
    "expected_cursor_final_response_bytes",
    "expected_latest_cursor_final_response_for_captured_prompt",
    "expected_latest_cursor_final_response_for_cursor_iteration",
    "expected_latest_cursor_final_response_for_review_iteration",
    "findings_review_json",
    "verify_captured_review_prompt_bytes",
]

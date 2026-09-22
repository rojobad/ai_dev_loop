"""Strict fake Bridge inventory traversal via public integration CLI only."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from tests.support.bridge_acceptance_constants import (
    CODEX_INVALID_REVIEW_STDOUT,
    CODEX_INVALID_REVIEW_STDOUT_WITH_THREAD,
    CODEX_SUCCESS_REVIEW_STDERR,
    CODEX_SUCCESS_REVIEW_STDOUT,
    CODEX_SUCCESS_REVIEW_STDOUT_WITH_THREAD,
    EXPECTED_SEQUENCE_MANIFEST_NAME,
    FINDINGS_ALPHA_REVIEW_JSON,
    FINDINGS_BETA_LARGE_REVIEW_JSON,
    FIX_PROMPT_ALPHA_BYTES,
    FIX_PROMPT_BETA_BYTES,
    INITIAL_PROMPT_BYTES,
    INVALID_REVIEW_RESPONSE_BYTES,
    MARKDOWN_ALPHA_BYTES,
    MARKDOWN_BETA_LARGE_BYTES,
    NO_FINDINGS_MARKDOWN_BYTES,
    NO_FINDINGS_REVIEW_JSON,
    PLAN_BYTES,
    USAGE_LIMIT_REVIEW_STDERR,
    USAGE_LIMIT_REVIEW_STDOUT,
    USAGE_LIMIT_REVIEW_STDOUT_WITH_THREAD,
    IndependentSequenceReportExpectation,
    assert_independent_sequence_report_content,
    verify_captured_review_prompt_bytes,
)
from tests.support.fake_bridge_client import PublicIntegrationBridgeClient


class _ChunkClient(Protocol):
    resource_requests: int

    def invoke(self, integration_args: list[str]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class HistoricalPromptExpectation:
    run_id: str
    attempt_id: str


@dataclass(frozen=True)
class RunStateExpectation:
    run_id: str
    allowed_states: frozenset[str]
    require_residual_risk: bool = False


@dataclass(frozen=True)
class BridgeInventoryExpectations:
    initial_prompt_bytes: bytes = INITIAL_PROMPT_BYTES
    expected_plan_bytes: bytes = PLAN_BYTES
    published_report_sha256: str = ""
    historical: HistoricalPromptExpectation | None = None
    required_run_states: tuple[RunStateExpectation, ...] = ()
    runs_requiring_retry_wait_history: frozenset[str] = frozenset()
    process_output_run_ids: frozenset[str] = frozenset()
    strict_review_content_run_ids: frozenset[str] = frozenset()
    strict_review_all_discovered: bool = False
    require_process_output_all_discovered: bool = False
    findings_use_large_markdown: bool = False
    prepared_sequence_id: str | None = None
    published_sequence_id: str | None = None
    min_discovered_runs: int = 3
    min_discovered_sequences: int = 2
    phase_min_run_counts: dict[int, int] = field(default_factory=dict)
    plan_non_empty: bool = True
    expected_sequence_report_name: str = EXPECTED_SEQUENCE_MANIFEST_NAME
    min_sequence_report_phases: int = 2
    published_sequence_report: IndependentSequenceReportExpectation | None = None


def discover_latest_codex_review_attempt_id(
    client: PublicIntegrationBridgeClient,
    run_id: str,
) -> str | None:
    reviews_payload = client.invoke(["run", "reviews", run_id])
    review_items = reviews_payload["data"]["items"]
    if review_items:
        return str(review_items[-1]["attemptId"])
    attempts_payload = client.invoke(["run", "attempts", run_id])
    items = attempts_payload["data"]["items"]
    for item in reversed(items):
        if str(item.get("component")) != "codex":
            continue
        if str(item.get("effectKind") or "") == "codex.resume_review":
            return str(item["attemptId"])
    return None


def discover_latest_codex_attempt_id(
    client: PublicIntegrationBridgeClient,
    run_id: str,
) -> str | None:
    return discover_latest_codex_review_attempt_id(client, run_id)


def _codex_attempt_effect_kind(
    client: PublicIntegrationBridgeClient,
    run_id: str,
    attempt_id: str,
) -> str | None:
    attempts_payload = client.invoke(["run", "attempts", run_id])
    for item in attempts_payload["data"]["items"]:
        if str(item["attemptId"]) == attempt_id:
            effect = item.get("effectKind")
            return str(effect) if effect is not None else None
    return None


def _expected_usage_limit_stdout(
    client: PublicIntegrationBridgeClient,
    run_id: str,
    attempt_id: str,
) -> bytes:
    effect = _codex_attempt_effect_kind(client, run_id, attempt_id)
    if effect == "codex.bootstrap_review":
        return USAGE_LIMIT_REVIEW_STDOUT_WITH_THREAD
    return USAGE_LIMIT_REVIEW_STDOUT


def _expected_invalid_stdout(
    client: PublicIntegrationBridgeClient,
    run_id: str,
    attempt_id: str,
) -> bytes:
    effect = _codex_attempt_effect_kind(client, run_id, attempt_id)
    if effect == "codex.bootstrap_review":
        return CODEX_INVALID_REVIEW_STDOUT_WITH_THREAD
    return CODEX_INVALID_REVIEW_STDOUT


def _expected_success_stdout(
    client: PublicIntegrationBridgeClient,
    run_id: str,
    attempt_id: str,
) -> bytes:
    effect = _codex_attempt_effect_kind(client, run_id, attempt_id)
    if effect == "codex.bootstrap_review":
        return CODEX_SUCCESS_REVIEW_STDOUT_WITH_THREAD
    return CODEX_SUCCESS_REVIEW_STDOUT


def _require_available(data: dict[str, Any], *, label: str) -> None:
    if not data.get("available"):
        reason = data.get("reason")
        raise AssertionError(f"{label} unavailable (reason={reason!r})")


def _fetch_all_chunks(
    client: _ChunkClient,
    integration_args: list[str],
    *,
    chunk_size: int,
    label: str,
    expected_bytes: bytes | None = None,
    require_multiple_chunks: bool = False,
) -> bytes:
    offset = 0
    parts: list[bytes] = []
    chunk_requests = 0
    requests_before = client.resource_requests
    while True:
        chunk_requests += 1
        if chunk_requests > 500:
            raise AssertionError(f"{label} chunk pagination exceeded 500 reads")
        payload = client.invoke(
            [
                *integration_args,
                "--offset",
                str(offset),
                "--limit",
                str(chunk_size),
            ]
        )
        data = payload["data"]
        assert isinstance(data, dict)
        _require_available(data, label=f"{label} chunk at offset {offset}")
        parts.append(base64.b64decode(str(data["contentBase64"])))
        if not data.get("hasMore"):
            break
        next_offset = data.get("nextOffset")
        assert next_offset is not None
        next_int = int(next_offset)
        assert next_int > offset, f"{label} offset stalled at {offset}"
        offset = next_int
    requests_made = client.resource_requests - requests_before
    if require_multiple_chunks and requests_made < 2:
        raise AssertionError(
            f"{label} required multi-chunk pagination but only {requests_made} request(s)"
        )
    joined = b"".join(parts)
    if expected_bytes is not None and joined != expected_bytes:
        raise AssertionError(
            f"{label} byte mismatch: expected {len(expected_bytes)} bytes, got {len(joined)}"
        )
    return joined


def _assert_process_stream(
    client: PublicIntegrationBridgeClient,
    *,
    run_id: str,
    attempt_id: str,
    stream: str,
    stream_info: dict[str, Any],
    expected_bytes: bytes,
    chunk_size: int,
    require_multiple_chunks: bool,
) -> None:
    _require_available(stream_info, label=f"process {stream} meta {run_id} {attempt_id}")
    _fetch_all_chunks(
        client,
        [
            "run",
            "output",
            run_id,
            "--attempt",
            attempt_id,
            "--stream",
            stream,
        ],
        chunk_size=chunk_size,
        label=f"output {run_id} {attempt_id} {stream}",
        expected_bytes=expected_bytes,
        require_multiple_chunks=require_multiple_chunks,
    )


def _load_review_prompt_bytes(
    client: PublicIntegrationBridgeClient,
    *,
    run_id: str,
    attempt_id: str,
    prompt_flag: dict[str, Any],
) -> bytes:
    _require_available(prompt_flag, label=f"review prompt meta {run_id} {attempt_id}")
    return _fetch_all_chunks(
        client,
        [
            "run",
            "review-content",
            run_id,
            "--attempt",
            attempt_id,
            "--kind",
            "prompt",
        ],
        chunk_size=512,
        label=f"review prompt {run_id} {attempt_id}",
    )


def _assert_review_prompt_capture(
    client: PublicIntegrationBridgeClient,
    *,
    run_id: str,
    attempt_id: str,
    detail_data: dict[str, Any],
    required: bool,
    forbidden_reason: str | None = None,
) -> None:
    prompt_flag = detail_data["content"]["prompt"]
    if not required:
        assert prompt_flag.get("available") is False, (
            f"unexpected review prompt availability for {run_id} {attempt_id}: {prompt_flag!r}"
        )
        assert prompt_flag.get("reason") == forbidden_reason
        return
    prompt_bytes = _load_review_prompt_bytes(
        client,
        run_id=run_id,
        attempt_id=attempt_id,
        prompt_flag=prompt_flag,
    )
    verify_captured_review_prompt_bytes(
        prompt_bytes,
        iteration=int(detail_data["iteration"]),
    )


def _assert_review_response_capture(
    client: PublicIntegrationBridgeClient,
    *,
    run_id: str,
    attempt_id: str,
    response_flag: dict[str, Any],
    expected_bytes: bytes | None,
    forbidden: bool,
    forbidden_reasons: frozenset[str] = frozenset(),
) -> None:
    if expected_bytes is not None:
        _require_available(response_flag, label=f"review response meta {run_id} {attempt_id}")
        _fetch_all_chunks(
            client,
            [
                "run",
                "review-content",
                run_id,
                "--attempt",
                attempt_id,
                "--kind",
                "response",
            ],
            chunk_size=4096,
            label=f"review response {run_id} {attempt_id}",
            expected_bytes=expected_bytes,
            require_multiple_chunks=len(expected_bytes) > 4096,
        )
        return
    if forbidden:
        assert response_flag.get("available") is False, (
            f"unexpected review response availability for {run_id} {attempt_id}: {response_flag!r}"
        )
        reason = response_flag.get("reason")
        assert reason in forbidden_reasons, (
            f"unexpected review response absence reason for {run_id} {attempt_id}: {reason!r}"
        )


def _assert_review_content(
    client: PublicIntegrationBridgeClient,
    *,
    run_id: str,
    attempt_id: str,
    detail_data: dict[str, Any],
    require_process_output: bool,
    findings_use_large_markdown: bool,
) -> None:
    content_flags = detail_data["content"]
    findings = detail_data.get("findingsCount")
    findings_count = int(findings) if findings is not None else 0
    result_state = str(detail_data.get("resultState", ""))

    process_output = detail_data["processOutput"]
    response_flag = content_flags["response"]

    if result_state == "invalid":
        _assert_review_prompt_capture(
            client,
            run_id=run_id,
            attempt_id=attempt_id,
            detail_data=detail_data,
            required=True,
        )
        _assert_review_response_capture(
            client,
            run_id=run_id,
            attempt_id=attempt_id,
            response_flag=response_flag,
            expected_bytes=INVALID_REVIEW_RESPONSE_BYTES,
            forbidden=False,
        )
        markdown_flag = content_flags["reviewMarkdown"]
        assert markdown_flag.get("available") is False
        assert markdown_flag.get("reason") == "invalid_result"
        if require_process_output:
            expected_invalid_stdout = _expected_invalid_stdout(client, run_id, attempt_id)
            _assert_process_stream(
                client,
                run_id=run_id,
                attempt_id=attempt_id,
                stream="stdout",
                stream_info=process_output["stdout"],
                expected_bytes=expected_invalid_stdout,
                chunk_size=32,
                require_multiple_chunks=len(expected_invalid_stdout) > 32,
            )
            _assert_process_stream(
                client,
                run_id=run_id,
                attempt_id=attempt_id,
                stream="stderr",
                stream_info=process_output["stderr"],
                expected_bytes=CODEX_SUCCESS_REVIEW_STDERR,
                chunk_size=32,
                require_multiple_chunks=False,
            )
        return

    if result_state not in {"valid", "invalid"}:
        _assert_review_prompt_capture(
            client,
            run_id=run_id,
            attempt_id=attempt_id,
            detail_data=detail_data,
            required=True,
        )
        _assert_review_response_capture(
            client,
            run_id=run_id,
            attempt_id=attempt_id,
            response_flag=response_flag,
            expected_bytes=None,
            forbidden=True,
            forbidden_reasons=frozenset({"not_produced", "not_yet_produced"}),
        )
        if require_process_output:
            expected_usage_stdout = _expected_usage_limit_stdout(client, run_id, attempt_id)
            _assert_process_stream(
                client,
                run_id=run_id,
                attempt_id=attempt_id,
                stream="stdout",
                stream_info=process_output["stdout"],
                expected_bytes=expected_usage_stdout,
                chunk_size=64,
                require_multiple_chunks=len(expected_usage_stdout) > 64,
            )
            _assert_process_stream(
                client,
                run_id=run_id,
                attempt_id=attempt_id,
                stream="stderr",
                stream_info=process_output["stderr"],
                expected_bytes=USAGE_LIMIT_REVIEW_STDERR,
                chunk_size=64,
                require_multiple_chunks=False,
            )
        return

    _assert_review_prompt_capture(
        client,
        run_id=run_id,
        attempt_id=attempt_id,
        detail_data=detail_data,
        required=True,
    )

    if findings_count == 0 and result_state == "valid":
        _require_available(response_flag, label=f"review response meta {run_id} {attempt_id}")
        _fetch_all_chunks(
            client,
            [
                "run",
                "review-content",
                run_id,
                "--attempt",
                attempt_id,
                "--kind",
                "response",
            ],
            chunk_size=256,
            label=f"review response {run_id} {attempt_id}",
            expected_bytes=NO_FINDINGS_REVIEW_JSON,
        )
        markdown_flag = content_flags["reviewMarkdown"]
        _require_available(markdown_flag, label=f"review markdown meta {run_id} {attempt_id}")
        _fetch_all_chunks(
            client,
            [
                "run",
                "review-content",
                run_id,
                "--attempt",
                attempt_id,
                "--kind",
                "review-markdown",
            ],
            chunk_size=512,
            label=f"review markdown {run_id} {attempt_id}",
            expected_bytes=NO_FINDINGS_MARKDOWN_BYTES,
        )
    elif findings_count > 0:
        expected_markdown = MARKDOWN_ALPHA_BYTES
        expected_response = FINDINGS_ALPHA_REVIEW_JSON
        expected_fix = FIX_PROMPT_ALPHA_BYTES
        if findings_use_large_markdown:
            expected_markdown = MARKDOWN_BETA_LARGE_BYTES
            expected_response = FINDINGS_BETA_LARGE_REVIEW_JSON
            expected_fix = FIX_PROMPT_BETA_BYTES
        markdown_flag = content_flags["reviewMarkdown"]
        _require_available(markdown_flag, label=f"review markdown meta {run_id} {attempt_id}")
        _fetch_all_chunks(
            client,
            [
                "run",
                "review-content",
                run_id,
                "--attempt",
                attempt_id,
                "--kind",
                "review-markdown",
            ],
            chunk_size=512,
            label=f"review markdown {run_id} {attempt_id}",
            expected_bytes=expected_markdown,
            require_multiple_chunks=len(expected_markdown) > 512,
        )
        _require_available(response_flag, label=f"review response meta {run_id} {attempt_id}")
        _fetch_all_chunks(
            client,
            [
                "run",
                "review-content",
                run_id,
                "--attempt",
                attempt_id,
                "--kind",
                "response",
            ],
            chunk_size=4096,
            label=f"review response {run_id} {attempt_id}",
            expected_bytes=expected_response,
            require_multiple_chunks=len(expected_response) > 4096,
        )
        fix_flag = content_flags["cursorFixPrompt"]
        _require_available(fix_flag, label=f"fix prompt meta {run_id} {attempt_id}")
        _fetch_all_chunks(
            client,
            [
                "run",
                "review-content",
                run_id,
                "--attempt",
                attempt_id,
                "--kind",
                "cursor-fix-prompt",
            ],
            chunk_size=128,
            label=f"fix prompt {run_id} {attempt_id}",
            expected_bytes=expected_fix,
        )

    if require_process_output and result_state == "valid":
        expected_success_stdout = _expected_success_stdout(client, run_id, attempt_id)
        _assert_process_stream(
            client,
            run_id=run_id,
            attempt_id=attempt_id,
            stream="stdout",
            stream_info=process_output["stdout"],
            expected_bytes=expected_success_stdout,
            chunk_size=32,
            require_multiple_chunks=len(expected_success_stdout) > 32,
        )
        _assert_process_stream(
            client,
            run_id=run_id,
            attempt_id=attempt_id,
            stream="stderr",
            stream_info=process_output["stderr"],
            expected_bytes=CODEX_SUCCESS_REVIEW_STDERR,
            chunk_size=32,
            require_multiple_chunks=False,
        )


def _strict_review_enabled(
    expectations: BridgeInventoryExpectations,
    run_id: str,
) -> bool:
    if expectations.strict_review_all_discovered:
        return True
    return run_id in expectations.strict_review_content_run_ids


def _process_output_required(
    expectations: BridgeInventoryExpectations,
    run_id: str,
) -> bool:
    if expectations.require_process_output_all_discovered:
        return True
    return run_id in expectations.process_output_run_ids


def _assert_run_budget_and_activity(
    client: PublicIntegrationBridgeClient,
    *,
    run_id: str,
    inspect_data: dict[str, Any],
    require_retry_history: bool,
) -> None:
    budget = inspect_data["reviewBudget"]
    completed = int(budget["completed"])
    maximum = int(budget["max"])
    assert 0 <= completed <= maximum
    assert maximum >= 1
    assert inspect_data["attemptCount"] >= 1
    timeline = client.invoke(["run", "timeline", run_id])
    assert timeline["data"]["items"]
    history = client.invoke(["run", "history", run_id])
    history_items = history["data"]["items"]
    assert history_items
    if require_retry_history:
        kinds = {str(item["kind"]) for item in history_items}
        retry_markers = {
            "codex_review_retryable_failure",
            "codex_review_retry_requested",
            "codex_review_blocked",
        }
        assert kinds & retry_markers, (
            f"run {run_id} history missing retry/wait evidence; kinds={sorted(kinds)!r}"
        )


def traverse_bridge_supervision_inventory(
    client: PublicIntegrationBridgeClient,
    *,
    expectations: BridgeInventoryExpectations,
) -> None:
    info = client.invoke(["info"])
    capabilities = info["data"]["capabilities"]
    assert capabilities["codexCapacity"] is True

    discovered_run_ids: set[str] = set()
    runs_payload = client.invoke(["runs", "list", "--kind", "all"])
    items = runs_payload["data"]["items"]
    assert len(items) >= expectations.min_discovered_runs, "runs list inventory too small"
    for item in items:
        discovered_run_ids.add(str(item["runId"]))

    discovered_sequence_ids: set[str] = set()
    sequences_payload = client.invoke(["sequences", "list"])
    seq_items = sequences_payload["data"]["items"]
    assert len(seq_items) >= expectations.min_discovered_sequences, (
        "sequences list inventory too small"
    )
    for item in seq_items:
        discovered_sequence_ids.add(str(item["sequenceId"]))

    if expectations.prepared_sequence_id is not None:
        assert expectations.prepared_sequence_id in discovered_sequence_ids

    if expectations.published_sequence_id is not None:
        assert expectations.published_sequence_id in discovered_sequence_ids

    if expectations.historical is not None:
        assert expectations.historical.run_id in discovered_run_ids

    for spec in expectations.required_run_states:
        assert spec.run_id in discovered_run_ids
        inspect = client.invoke(["run", "inspect", spec.run_id])
        data = inspect["data"]
        assert data["state"] in spec.allowed_states, (
            f"run {spec.run_id} state {data['state']!r} not in {spec.allowed_states!r}"
        )
        if spec.require_residual_risk:
            assert data.get("residualRisk") is True

    for run_id in sorted(discovered_run_ids):
        inspect = client.invoke(["run", "inspect", run_id])
        data = inspect["data"]
        assert data["runId"] == run_id
        assert "reviewBudget" in data
        client.invoke(["run", "attempts", run_id])
        _assert_run_budget_and_activity(
            client,
            run_id=run_id,
            inspect_data=data,
            require_retry_history=run_id in expectations.runs_requiring_retry_wait_history,
        )

        _fetch_all_chunks(
            client,
            ["run", "initial-prompt", run_id],
            chunk_size=4096,
            label=f"initial-prompt {run_id}",
            expected_bytes=expectations.initial_prompt_bytes,
        )

        if expectations.plan_non_empty:
            _fetch_all_chunks(
                client,
                ["run", "plan", run_id],
                chunk_size=4096,
                label=f"plan {run_id}",
                expected_bytes=expectations.expected_plan_bytes,
            )

        reviews = client.invoke(["run", "reviews", run_id])
        assert reviews["data"]["items"] is not None
        if not _strict_review_enabled(expectations, run_id):
            continue
        require_process = _process_output_required(expectations, run_id)
        for item in reviews["data"]["items"]:
            attempt_id = str(item["attemptId"])
            detail = client.invoke(["run", "review", run_id, "--attempt", attempt_id])
            detail_data = detail["data"]
            if (
                expectations.historical is not None
                and run_id == expectations.historical.run_id
                and attempt_id == expectations.historical.attempt_id
            ):
                prompt_meta = detail_data["content"]["prompt"]
                assert prompt_meta.get("available") is False
                assert prompt_meta.get("reason") == "not_recorded"
                continue
            _assert_review_content(
                client,
                run_id=run_id,
                attempt_id=attempt_id,
                detail_data=detail_data,
                require_process_output=require_process,
                findings_use_large_markdown=expectations.findings_use_large_markdown,
            )

    for sequence_id in sorted(discovered_sequence_ids):
        inspect = client.invoke(["sequence", "inspect", sequence_id])
        seq = inspect["data"]
        assert seq["sequenceId"] == sequence_id
        report_meta = seq["report"]
        if sequence_id == expectations.published_sequence_id:
            _require_available(report_meta, label=f"published report {sequence_id}")
            report_bytes = _fetch_all_chunks(
                client,
                ["sequence", "report", sequence_id],
                chunk_size=512,
                label=f"sequence report {sequence_id}",
            )
            digest = hashlib.sha256(report_bytes).hexdigest()
            assert digest == expectations.published_report_sha256
            if expectations.published_sequence_report is not None:
                assert_independent_sequence_report_content(
                    report_bytes,
                    expectation=expectations.published_sequence_report,
                )
            prefix = report_meta.get("sha256Prefix")
            if prefix:
                assert digest.startswith(str(prefix))
        elif report_meta.get("available"):
            _fetch_all_chunks(
                client,
                ["sequence", "report", sequence_id],
                chunk_size=8192,
                label=f"sequence report {sequence_id}",
            )
        else:
            reason = report_meta.get("reason")
            assert reason in {"not_yet_produced", "publication_pending"}, (
                f"unexpected report absence for {sequence_id}: {reason!r}"
            )

        for phase in seq["phases"]:
            ordinal = int(phase["ordinal"])
            min_runs = expectations.phase_min_run_counts.get(ordinal, 0)
            offset = 0
            run_count = 0
            page_guard = 0
            while True:
                page_guard += 1
                if page_guard > 50:
                    raise AssertionError(
                        f"phase-runs pagination stuck for {sequence_id} ordinal {ordinal}"
                    )
                page = client.invoke(
                    [
                        "sequence",
                        "phase-runs",
                        sequence_id,
                        "--ordinal",
                        str(ordinal),
                        "--offset",
                        str(offset),
                        "--limit",
                        "100",
                    ]
                )
                page_data = page["data"]
                run_count += len(page_data["items"])
                if phase["frozenInputs"]["planAvailable"]:
                    _fetch_all_chunks(
                        client,
                        [
                            "sequence",
                            "phase-plan",
                            sequence_id,
                            "--ordinal",
                            str(ordinal),
                        ],
                        chunk_size=4096,
                        label=f"phase-plan {sequence_id} {ordinal}",
                        expected_bytes=expectations.expected_plan_bytes,
                    )
                if phase["frozenInputs"]["promptAvailable"]:
                    _fetch_all_chunks(
                        client,
                        [
                            "sequence",
                            "phase-prompt",
                            sequence_id,
                            "--ordinal",
                            str(ordinal),
                        ],
                        chunk_size=4096,
                        label=f"phase-prompt {sequence_id} {ordinal}",
                        expected_bytes=expectations.initial_prompt_bytes,
                    )
                if not page_data["page"]["hasMore"]:
                    break
                next_offset = page_data["page"]["nextOffset"]
                assert next_offset is not None
                next_int = int(next_offset)
                assert next_int > offset, f"phase-runs offset stalled at {offset}"
                offset = next_int
            if sequence_id == expectations.published_sequence_id:
                assert run_count >= min_runs, (
                    f"sequence {sequence_id} phase {ordinal} expected >={min_runs} runs, "
                    f"got {run_count}"
                )
            elif sequence_id == expectations.prepared_sequence_id:
                assert run_count == 0, (
                    f"prepared sequence phase {ordinal} expected zero runs, got {run_count}"
                )

    capacity = client.invoke(["codex-capacity"])
    assert capacity["data"]["status"] in {"available", "exhausted", "unavailable"}
    assert "limits" in capacity["data"]

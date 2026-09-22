"""Phase 21.6 full Integration API acceptance via public CLI only."""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from tests.integration.phase21_4_helpers import (
    BOOTSTRAP_ID,
    install_pre_21_4_historical_review_fixture,
    make_tick_service,
    run_tick_once,
    run_until,
    submit_sample_run,
)
from tests.integration.test_phase21_3_sequence_inspection import (
    _awaiting_finalization_after_retry_successor,
)
from tests.support.bridge_acceptance_constants import (
    BRIDGE_CHILD_STDERR_SENTINEL,
    BRIDGE_CHILD_STDOUT_SENTINEL,
    CODEX_SUCCESS_REVIEW_STDERR,
    EXPECTED_SEQUENCE_MANIFEST_NAME,
    INITIAL_PROMPT_BYTES,
    LARGE_MARKDOWN_PAD_BYTES,
    IndependentSequenceReportExpectation,
)
from tests.support.bridge_supervision_traversal import (
    BridgeInventoryExpectations,
    RunStateExpectation,
    _expected_success_stdout,
    _fetch_all_chunks,
    discover_latest_codex_review_attempt_id,
    traverse_bridge_supervision_inventory,
)
from tests.support.fake_bridge_client import PublicIntegrationBridgeClient
from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_NOW
from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence

from ai_dev_loop.integration_api.consumer import ConsumerGate, evaluate_envelope_major
from ai_dev_loop.integration_api.schemas import (
    CODEX_CAPACITY_DATA_SCHEMA,
    INFO_DATA_SCHEMA,
    validate_integration_instance,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.sequence_report import (
    SEQUENCE_COMPLETION_REPORT_ARTIFACT,
    reconcile_completion_report_publication,
)
from ai_dev_loop.scheduler.application.start import start_run

FIXTURES_V1_0 = Path(__file__).resolve().parents[1] / "fixtures" / "integration_api" / "v1_0"
FIXTURES_V1_1 = Path(__file__).resolve().parents[1] / "fixtures" / "integration_api" / "v1_1"
FIXTURES_V1_5 = Path(__file__).resolve().parents[1] / "fixtures" / "integration_api" / "v1_5"
SECRET_SENTINEL = "INTEGRATION_PHASE21_6_ACCEPT_SECRET"


def _pinned_v1_0_required_field_consumer(data: dict[str, object]) -> None:
    """Pinned Bridge consumer from API 1.0: only required top-level fields."""

    assert isinstance(data.get("aiDevLoopVersion"), str)
    caps = data.get("capabilities")
    assert isinstance(caps, dict)
    for key in ("runs", "sequences", "reviewInspection", "processOutput"):
        assert isinstance(caps[key], bool)


def _collect_sequence_run_ids(
    client: PublicIntegrationBridgeClient,
    sequence_id: str,
    *,
    ordinals: tuple[int, ...],
) -> frozenset[str]:
    run_ids: set[str] = set()
    for ordinal in ordinals:
        offset = 0
        for _ in range(50):
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
            for item in page_data["items"]:
                run_ids.add(str(item["runId"]))
            if not page_data["page"]["hasMore"]:
                break
            offset = int(page_data["page"]["nextOffset"])
    return frozenset(run_ids)


def _drive_live_codex_output_via_bridge(
    client: PublicIntegrationBridgeClient,
    tick: object,
    *,
    run_id: str,
    release_path: Path,
) -> str:
    tick_error: list[BaseException] = []
    stop_ticks = threading.Event()

    def _drive_ticks() -> None:
        try:
            while not stop_ticks.is_set():
                run_tick_once(tick)  # type: ignore[arg-type]
                time.sleep(0.02)
        except BaseException as exc:
            tick_error.append(exc)
        finally:
            stop_ticks.set()

    driver = threading.Thread(target=_drive_ticks, daemon=True)
    driver.start()
    attempt_id: str | None = None
    saw_live_stdout = False
    saw_live_stderr = False
    deadline = time.monotonic() + 45.0
    try:
        while time.monotonic() < deadline:
            attempt_id = discover_latest_codex_review_attempt_id(client, run_id)
            if attempt_id is None:
                time.sleep(0.05)
                continue
            stdout_payload = client.invoke(
                [
                    "run",
                    "output",
                    run_id,
                    "--attempt",
                    attempt_id,
                    "--stream",
                    "stdout",
                ]
            )
            stdout_data = stdout_payload["data"]
            if stdout_data.get("available") and stdout_data.get("complete") is False:
                stdout_partial = base64.b64decode(str(stdout_data["contentBase64"]))
                if stdout_partial.startswith(BRIDGE_CHILD_STDOUT_SENTINEL):
                    saw_live_stdout = True
            stderr_payload = client.invoke(
                [
                    "run",
                    "output",
                    run_id,
                    "--attempt",
                    attempt_id,
                    "--stream",
                    "stderr",
                ]
            )
            stderr_data = stderr_payload["data"]
            if stderr_data.get("available") and stderr_data.get("complete") is False:
                stderr_partial = base64.b64decode(str(stderr_data["contentBase64"]))
                if stderr_partial.startswith(BRIDGE_CHILD_STDERR_SENTINEL):
                    saw_live_stderr = True
            if saw_live_stdout and saw_live_stderr:
                release_path.write_text("go", encoding="utf-8")
                break
            time.sleep(0.05)
    finally:
        stop_ticks.set()
        if not release_path.is_file():
            release_path.write_text("go", encoding="utf-8")
        driver.join(timeout=10.0)
        assert not driver.is_alive()
    if tick_error:
        raise tick_error[0]
    assert attempt_id is not None
    assert saw_live_stdout
    assert saw_live_stderr
    return attempt_id


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _bridge_env(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, str]:
    return {
        **dict(os.environ),
        "PATH": os.pathsep.join([str(fake_clis["bin_dir"]), os.environ.get("PATH", "")]),
        "XDG_STATE_HOME": str(isolated_xdg / "state"),
        "XDG_CONFIG_HOME": str(isolated_xdg / "config"),
        "XDG_CACHE_HOME": str(isolated_xdg / "cache"),
        "FAKE_CODEX_CAPACITY": "available",
        "FAKE_CODEX_CAPACITY_SHAPE": "primary_only",
    }


def test_c04_fake_bridge_client_has_no_private_imports() -> None:
    client_path = Path(__file__).resolve().parents[1] / "support" / "fake_bridge_client.py"
    tree = ast.parse(client_path.read_text(encoding="utf-8"))
    banned = {
        "ai_dev_loop.scheduler.infrastructure.sqlite_store",
        "ai_dev_loop.integration_api.run_service",
        "sqlite3",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in banned
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module not in banned


def test_c04_historical_prompt_not_recorded_via_public_cli(
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hist_state = isolated_xdg / "historical" / "state" / "ai_dev_loop"
    hist_paths = {
        "db_path": hist_state / "engine.sqlite3",
        "artifact_root": hist_state / "artifacts",
    }
    historical = install_pre_21_4_historical_review_fixture(hist_paths)
    env = {
        **dict(os.environ),
        "PATH": os.pathsep.join([str(fake_clis["bin_dir"]), os.environ.get("PATH", "")]),
        "XDG_STATE_HOME": str(isolated_xdg / "historical" / "state"),
        "XDG_CONFIG_HOME": str(isolated_xdg / "historical" / "config"),
        "XDG_CACHE_HOME": str(isolated_xdg / "historical" / "cache"),
    }
    client = PublicIntegrationBridgeClient(env=env)
    payload = client.invoke(
        [
            "run",
            "review-content",
            str(historical["run_id"]),
            "--attempt",
            str(historical["attempt_id"]),
            "--kind",
            "prompt",
        ]
    )
    assert payload["data"]["available"] is False
    assert payload["data"]["reason"] == "not_recorded"
    assert client.resource_requests == 1


def test_c04_residual_risk_run_inspect_via_public_cli(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_xdg / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(isolated_xdg / "cache"))
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "blocked_environment")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = make_tick_service(git_repo, scheduler_paths)
    run_until(tick, run_id, target_kind="completed_with_residual_risk", max_ticks=120)
    client = PublicIntegrationBridgeClient(env=_bridge_env(isolated_xdg, fake_clis))
    inspect = client.invoke(["run", "inspect", run_id])
    assert inspect["data"]["state"] == "completed_with_residual_risk"
    assert inspect["data"]["residualRisk"] is True


def test_c04_waiting_codex_capacity_run_inspect_via_public_cli(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_xdg / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(isolated_xdg / "cache"))
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = make_tick_service(git_repo, scheduler_paths)
    run_until(tick, run_id, target_kind="waiting_codex_capacity", max_ticks=120)
    client = PublicIntegrationBridgeClient(env=_bridge_env(isolated_xdg, fake_clis))
    inspect = client.invoke(["run", "inspect", run_id])
    assert inspect["data"]["state"] == "waiting_codex_capacity"


def test_c04_live_codex_stdout_before_and_after_release_via_bridge_client(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_xdg / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(isolated_xdg / "cache"))
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv(
        "FAKE_CODEX_CHILD_STDOUT_SENTINEL",
        BRIDGE_CHILD_STDOUT_SENTINEL.decode("utf-8"),
    )
    monkeypatch.setenv(
        "FAKE_CODEX_CHILD_STDERR_SENTINEL",
        BRIDGE_CHILD_STDERR_SENTINEL.decode("utf-8"),
    )
    release_path = tmp_path / "bridge-live-release"
    monkeypatch.setenv("FAKE_CODEX_CHILD_RELEASE_FILE", str(release_path))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    client = PublicIntegrationBridgeClient(env=_bridge_env(isolated_xdg, fake_clis))
    run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=80)
    _drive_live_codex_output_via_bridge(
        client,
        tick,
        run_id=run_id,
        release_path=release_path,
    )
    run_until(tick, run_id, target_kind="completed", max_ticks=120)
    completed_attempt = discover_latest_codex_review_attempt_id(client, run_id)
    assert completed_attempt is not None
    expected_stdout = _expected_success_stdout(client, run_id, completed_attempt)
    _fetch_all_chunks(
        client,
        [
            "run",
            "output",
            run_id,
            "--attempt",
            completed_attempt,
            "--stream",
            "stdout",
        ],
        chunk_size=32,
        label=f"live stdout {run_id}",
        expected_bytes=expected_stdout,
    )
    _fetch_all_chunks(
        client,
        [
            "run",
            "output",
            run_id,
            "--attempt",
            completed_attempt,
            "--stream",
            "stderr",
        ],
        chunk_size=32,
        label=f"live stderr {run_id}",
        expected_bytes=CODEX_SUCCESS_REVIEW_STDERR,
    )


def test_c04_zero_run_phase_via_public_cli(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_xdg / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(isolated_xdg / "cache"))
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    client = PublicIntegrationBridgeClient(env=_bridge_env(isolated_xdg, fake_clis))
    page = client.invoke(
        [
            "sequence",
            "phase-runs",
            sequence_id,
            "--ordinal",
            "1",
            "--offset",
            "0",
            "--limit",
            "10",
        ]
    )
    assert page["data"]["items"] == []


def test_c04_full_supervision_traversal_public_cli_only(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_xdg / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(isolated_xdg / "cache"))
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv(
        "FAKE_CODEX_CHILD_STDOUT_SENTINEL",
        BRIDGE_CHILD_STDOUT_SENTINEL.decode("utf-8"),
    )
    monkeypatch.setenv(
        "FAKE_CODEX_CHILD_STDERR_SENTINEL",
        BRIDGE_CHILD_STDERR_SENTINEL.decode("utf-8"),
    )

    sequence_id, awaiting, store, artifacts = _awaiting_finalization_after_retry_successor(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    published = reconcile_completion_report_publication(
        store,
        artifacts,
        awaiting,
        now=FIXED_NOW + timedelta(hours=1),
    )
    report_path = artifacts.sequence_root(sequence_id) / SEQUENCE_COMPLETION_REPORT_ARTIFACT
    report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
    assert published.completion_report_sha256 == report_sha256

    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "invalid_json,findings,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_LARGE_MARKDOWN", "1")
    monkeypatch.setenv("FAKE_CODEX_LARGE_MARKDOWN_BYTES", str(LARGE_MARKDOWN_PAD_BYTES))

    standalone_counter = isolated_xdg / "standalone-codex-review-counter.txt"
    monkeypatch.setenv("FAKE_CODEX_REVIEW_COUNTER", str(standalone_counter))

    def unique_attempt_id() -> str:
        return f"att-{secrets.token_hex(16)}"

    standalone_run_id = submit_sample_run(git_repo, scheduler_paths)
    start_run(standalone_run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    standalone_tick = make_tick_service(
        git_repo,
        scheduler_paths,
        attempt_id_factory=unique_attempt_id,
        backend=backend,
    )
    bridge_client = PublicIntegrationBridgeClient(env=_bridge_env(isolated_xdg, fake_clis))
    run_until(standalone_tick, standalone_run_id, target_kind="completed", max_ticks=120)

    sequence_run_ids = _collect_sequence_run_ids(
        bridge_client,
        sequence_id,
        ordinals=(1, 2),
    )
    materialized_run_ids = {entry.ordinal: entry.run_id for entry in awaiting.materialized_entries}
    traverse_bridge_supervision_inventory(
        bridge_client,
        expectations=BridgeInventoryExpectations(
            initial_prompt_bytes=INITIAL_PROMPT_BYTES,
            published_report_sha256=report_sha256,
            prepared_sequence_id=None,
            published_sequence_id=sequence_id,
            published_sequence_report=IndependentSequenceReportExpectation(
                sequence_id=sequence_id,
                sequence_name=EXPECTED_SEQUENCE_MANIFEST_NAME,
                final_outcome="completed",
                final_run_id=awaiting.final_run_id,
                run_ids_by_ordinal=materialized_run_ids,
            ),
            min_discovered_runs=3,
            min_discovered_sequences=1,
            phase_min_run_counts={1: 2, 2: 1},
            runs_requiring_retry_wait_history=frozenset({standalone_run_id}),
            strict_review_all_discovered=True,
            require_process_output_all_discovered=True,
            findings_use_large_markdown=True,
            required_run_states=(
                RunStateExpectation(
                    run_id=standalone_run_id,
                    allowed_states=frozenset({"completed"}),
                ),
            ),
        ),
    )
    assert sequence_run_ids
    assert bridge_client.resource_requests > 0
    assert SECRET_SENTINEL not in json.dumps(bridge_client.invoke(["info"]))


def test_c05_pinned_v1_0_consumer_tolerates_additive_fields_on_fixture() -> None:
    """Schema validation on a saved envelope does not prove a pinned consumer accepts live 1.5."""

    payload = json.loads((FIXTURES_V1_0 / "info_success_envelope.json").read_text(encoding="utf-8"))
    payload["data"]["capabilities"]["futureFeature"] = True
    payload["data"]["capabilities"]["codexCapacity"] = True
    payload["unknownTopLevel"] = True
    assert evaluate_envelope_major(payload) is ConsumerGate.ACCEPT
    _pinned_v1_0_required_field_consumer(payload["data"])
    validate_integration_instance(payload["data"], INFO_DATA_SCHEMA)


def test_c05_pinned_v1_0_consumer_accepts_live_api_info(
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    client = PublicIntegrationBridgeClient(env=_bridge_env(isolated_xdg, fake_clis))
    info = client.invoke(["info"])
    assert info["apiVersion"]["major"] == 1
    assert int(info["apiVersion"]["minor"]) >= 5
    assert evaluate_envelope_major(info) is ConsumerGate.ACCEPT
    _pinned_v1_0_required_field_consumer(info["data"])
    validate_integration_instance(info["data"], INFO_DATA_SCHEMA)


def test_c05_v1_1_minimal_run_list_historical_shape() -> None:
    from ai_dev_loop.integration_api.schemas import RUN_LIST_DATA_SCHEMA

    payload = json.loads((FIXTURES_V1_1 / "run_list_data_minimal.json").read_text(encoding="utf-8"))
    validate_integration_instance(payload, RUN_LIST_DATA_SCHEMA)


def test_c05_v1_5_capacity_fixture_current_schema() -> None:
    payload = json.loads(
        (FIXTURES_V1_5 / "codex_capacity_available_primary_only.json").read_text(encoding="utf-8")
    )
    validate_integration_instance(payload, CODEX_CAPACITY_DATA_SCHEMA)
    assert payload["limits"][0]["resetsAt"] is None


def test_c05_major_mismatch_blocks_resource_requests_on_fixture() -> None:
    mismatch = json.loads(
        (FIXTURES_V1_0 / "envelope_major_mismatch.json").read_text(encoding="utf-8")
    )
    assert evaluate_envelope_major(mismatch) is ConsumerGate.UPDATE_REQUIRED
    calls = 0

    def resource() -> None:
        nonlocal calls
        calls += 1

    from ai_dev_loop.integration_api.consumer import run_gated_resource_request

    gate = run_gated_resource_request(mismatch, resource_request=resource)
    assert gate.value == "UPDATE_REQUIRED"
    assert calls == 0


def test_c05_bridge_client_stops_after_info_major_mismatch_envelope(
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _bridge_env(isolated_xdg, fake_clis)
    mismatch_body = (FIXTURES_V1_0 / "envelope_major_mismatch.json").read_text(encoding="utf-8")
    real_run = subprocess.run

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = kwargs.get("args") if "args" in kwargs else (args[0] if args else None)
        if isinstance(argv, list) and "integration" in argv and "info" in argv:
            return subprocess.CompletedProcess(argv, 0, mismatch_body, "")
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = PublicIntegrationBridgeClient(env=env)
    info_payload = client.invoke(["info"])
    assert evaluate_envelope_major(info_payload) is ConsumerGate.UPDATE_REQUIRED
    assert client.resource_requests == 0
    with pytest.raises(AssertionError, match="UPDATE_REQUIRED"):
        client.invoke(["runs", "list"])

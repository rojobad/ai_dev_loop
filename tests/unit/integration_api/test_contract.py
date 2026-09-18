"""Unit tests for Integration API contract foundation (Phase 21.1)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest
import typer.main
from pydantic import ValidationError
from typer.testing import CliRunner

from ai_dev_loop import __version__
from ai_dev_loop.cli import app
from ai_dev_loop.commands.integration import integration_app, run_integration_cli
from ai_dev_loop.integration_api.clock import set_observed_at_provider
from ai_dev_loop.integration_api.consumer import (
    UPDATE_REQUIRED,
    ConsumerGate,
    evaluate_envelope_major,
    run_gated_resource_request,
)
from ai_dev_loop.integration_api.envelope import (
    build_failure_envelope,
    build_success_envelope,
)
from ai_dev_loop.integration_api.errors import IntegrationApiError, IntegrationErrorCode
from ai_dev_loop.integration_api.info import build_integration_info_data
from ai_dev_loop.integration_api.models import (
    ApiVersion,
    IntegrationCapabilities,
    IntegrationEnvelope,
)
from ai_dev_loop.integration_api.schemas import (
    ARTIFACT_CHUNK_SCHEMA,
    COLLECTION_PAGE_SCHEMA,
    ENVELOPE_SCHEMA,
    INFO_DATA_SCHEMA,
    assert_packaged_schemas_exist,
    validate_integration_instance,
)
from ai_dev_loop.integration_api.validation import (
    COLLECTION_HARD_MAX_LIMIT,
    parse_observed_at_rfc3339_utc,
    validate_artifact_chunk_wire,
    validate_collection_bounds,
    validate_collection_page_meta_wire,
    validate_envelope_wire_payload,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "integration_api" / "v1_0"
SECRET_SENTINEL = "INTEGRATION_TEST_SECRET_SENTINEL_DO_NOT_LEAK"
PRIVATE_SENTINEL = "PRIVATE_INTEGRATION_SENTINEL_XYZ"
REPO_ROOT = Path(__file__).resolve().parents[3]
runner = CliRunner()


def _load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    if not root.exists():
        return {}
    snapshot: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        snapshot[str(path.relative_to(root))] = (stat.st_mode, stat.st_size)
    return snapshot


@pytest.fixture(autouse=True)
def _reset_observed_at_provider() -> None:
    set_observed_at_provider(None)
    yield
    set_observed_at_provider(None)


def _invoke_integration(args: list[str]) -> tuple[int, str, str]:
    buffer = StringIO()
    stderr = StringIO()
    with patch("sys.stdout", buffer), patch("sys.stderr", stderr):
        code = run_integration_cli(args)
    return code, buffer.getvalue(), stderr.getvalue()


def test_f01_info_default_and_explicit_json_output() -> None:
    for args in (["info"], ["info", "--output", "json"]):
        code, stdout, _stderr = _invoke_integration(args)
        assert code == 0
        payload = json.loads(stdout)
        assert payload["ok"] is True
        assert payload["data"]["aiDevLoopVersion"] == __version__


def test_f01_invalid_and_missing_output_values() -> None:
    code, stdout, _stderr = _invoke_integration(["info", "--output", "text"])
    assert code == 2
    assert json.loads(stdout)["error"]["code"] == "INVALID_ARGUMENT"

    code, stdout, _stderr = _invoke_integration(["info", "--output"])
    assert code == 2
    assert json.loads(stdout)["error"]["code"] == "INVALID_ARGUMENT"
    assert SECRET_SENTINEL not in stdout


def test_f02_clirunner_root_and_namespace_help() -> None:
    root = runner.invoke(app, ["--help"])
    assert root.exit_code == 0
    assert "integration" in root.stdout
    assert "integrations" in root.stdout

    singular = runner.invoke(app, ["integration", "--help"])
    assert singular.exit_code == 0
    assert "info" in singular.stdout

    plural = runner.invoke(app, ["integrations", "--help"])
    assert plural.exit_code == 0
    assert "install" in plural.stdout

    info = runner.invoke(app, ["integration", "info"])
    assert info.exit_code == 0
    payload = json.loads(info.stdout)
    assert payload["apiVersion"] == {"major": 1, "minor": 0}


def test_c01_entry_point_subprocess_smoke(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {
        **dict(os.environ),
        "XDG_STATE_HOME": str(isolated_xdg / "state"),
        "XDG_CONFIG_HOME": str(isolated_xdg / "config"),
        "XDG_CACHE_HOME": str(isolated_xdg / "cache"),
    }
    result = subprocess.run(
        ["uv", "run", "python", "-m", "ai_dev_loop.cli", "integration", "info"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    assert SECRET_SENTINEL not in result.stdout
    assert SECRET_SENTINEL not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["apiVersion"]["major"] == 1


def test_c01_info_cli_envelope_and_capabilities() -> None:
    fixed = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    set_observed_at_provider(lambda: fixed)
    code, stdout, _stderr = _invoke_integration(["info"])
    assert code == 0
    payload = json.loads(stdout)
    assert payload["apiVersion"] == {"major": 1, "minor": 0}
    assert payload["observedAt"] == "2026-09-18T12:00:00Z"
    assert payload["data"]["capabilities"]["runs"] is False


def test_f03_strict_integer_and_boolean_fields() -> None:
    with pytest.raises(ValidationError):
        ApiVersion.model_validate({"major": True, "minor": 0})
    with pytest.raises(ValidationError):
        ApiVersion.model_validate({"major": "1", "minor": 0})
    with pytest.raises(ValidationError):
        IntegrationCapabilities.model_validate({"runs": 1})

    assert parse_observed_at_rfc3339_utc("2026-09-18T12:00:00Z").year == 2026
    with pytest.raises(IntegrationApiError):
        parse_observed_at_rfc3339_utc("2026-09-18T12:00:00+00:00")


def _assert_single_invalid_argument_json(result: object, sentinel: str) -> None:
    from click.testing import Result
    from typer.testing import Result as TyperResult

    assert isinstance(result, (Result, TyperResult))
    assert result.exit_code == 2
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr


def test_f08_integration_group_parse_errors_via_root_app() -> None:
    _assert_single_invalid_argument_json(
        runner.invoke(app, ["integration", f"--{PRIVATE_SENTINEL}"]),
        PRIVATE_SENTINEL,
    )
    _assert_single_invalid_argument_json(
        runner.invoke(app, ["integration", "info", f"--{PRIVATE_SENTINEL}"]),
        PRIVATE_SENTINEL,
    )
    _assert_single_invalid_argument_json(
        runner.invoke(app, ["integration", "info", "--output"]),
        PRIVATE_SENTINEL,
    )
    _assert_single_invalid_argument_json(
        runner.invoke(app, ["integration", f"unknown-{PRIVATE_SENTINEL}"]),
        PRIVATE_SENTINEL,
    )


def test_f04_artifact_and_collection_invariants() -> None:
    validate_artifact_chunk_wire(_load_fixture("artifact_chunk_available_empty.json"))
    validate_artifact_chunk_wire(_load_fixture("artifact_chunk_unavailable.json"))
    validate_integration_instance(
        _load_fixture("artifact_chunk_available_empty.json"),
        ARTIFACT_CHUNK_SCHEMA,
    )
    with pytest.raises(IntegrationApiError):
        validate_artifact_chunk_wire(
            _load_fixture("artifact_chunk_invalid_unavailable_with_bytes.json")
        )
    validate_collection_page_meta_wire(
        {"offset": 0, "limit": 100, "nextOffset": None, "hasMore": False}
    )
    with pytest.raises(IntegrationApiError):
        validate_collection_bounds(0, COLLECTION_HARD_MAX_LIMIT + 1)


def test_f04_timestamp_and_collection_semantics() -> None:
    with pytest.raises(IntegrationApiError):
        parse_observed_at_rfc3339_utc("2026-02-30T12:00:00Z")
    with pytest.raises(IntegrationApiError):
        validate_envelope_wire_payload(
            _load_fixture("envelope_invalid_observed_at_impossible.json")
        )
    with pytest.raises(IntegrationApiError):
        validate_envelope_wire_payload(_load_fixture("envelope_invalid_observed_at_no_z.json"))
    with pytest.raises(ValidationError):
        IntegrationEnvelope.model_validate(_load_fixture("envelope_invalid_observed_at_no_z.json"))
    with pytest.raises(jsonschema.ValidationError):
        validate_integration_instance(
            _load_fixture("envelope_invalid_observed_at_impossible.json"),
            ENVELOPE_SCHEMA,
        )
    with pytest.raises(IntegrationApiError):
        validate_collection_page_meta_wire(
            _load_fixture("collection_invalid_next_offset_without_more.json")
        )
    with pytest.raises(jsonschema.ValidationError):
        validate_integration_instance(
            _load_fixture("collection_invalid_next_offset_without_more.json"),
            COLLECTION_PAGE_SCHEMA,
        )


def test_c03_consumer_same_major_and_unknown_fields() -> None:
    for name in ("info_success_envelope.json", "envelope_unknown_additive_fields.json"):
        payload = _load_fixture(name)
        assert evaluate_envelope_major(payload) is ConsumerGate.ACCEPT
        validate_integration_instance(payload, ENVELOPE_SCHEMA)

    mismatch = _load_fixture("envelope_major_mismatch.json")
    assert evaluate_envelope_major(mismatch) is ConsumerGate.UPDATE_REQUIRED

    calls = 0

    def resource() -> None:
        nonlocal calls
        calls += 1

    gate = run_gated_resource_request(mismatch, resource_request=resource)
    assert gate.value == UPDATE_REQUIRED
    assert calls == 0


def test_c03_fixture_rejects_boolean_where_integer_required() -> None:
    envelope = _load_fixture("info_success_envelope.json")
    envelope["apiVersion"]["major"] = True
    assert evaluate_envelope_major(envelope) is ConsumerGate.UPDATE_REQUIRED


def test_c03_info_schema_validates_fixture_data() -> None:
    payload = _load_fixture("info_success_envelope.json")["data"]
    validate_integration_instance(payload, INFO_DATA_SCHEMA)


def test_f05_keyboard_interrupt_not_success_or_internal_error() -> None:
    from typer import _click as click

    command = typer.main.get_command(integration_app)
    buffer = StringIO()
    with (
        patch("sys.stdout", buffer),
        patch("typer.main.get_command", return_value=command),
        patch.object(command, "main", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        run_integration_cli(["info"])
    assert buffer.getvalue() == ""

    buffer = StringIO()
    with (
        patch("sys.stdout", buffer),
        patch("typer.main.get_command", return_value=command),
        patch.object(command, "main", side_effect=click.exceptions.Exit(130)),
        pytest.raises(KeyboardInterrupt),
    ):
        run_integration_cli(["info"])
    assert buffer.getvalue() == ""
    assert '"ok":true' not in buffer.getvalue()


def test_f06_io_internal_errors_and_sentinel_scrubbing() -> None:
    with (
        patch(
            "ai_dev_loop.commands.integration.build_integration_info_data",
            side_effect=OSError(f"disk {SECRET_SENTINEL}"),
        ),
    ):
        code, stdout, stderr = _invoke_integration(["info"])
    assert code == 6
    payload = json.loads(stdout)
    assert payload["error"]["code"] == "IO_ERROR"
    assert SECRET_SENTINEL not in stdout
    assert SECRET_SENTINEL not in stderr

    with patch(
        "ai_dev_loop.commands.integration.build_integration_info_data",
        side_effect=RuntimeError(f"boom {SECRET_SENTINEL}"),
    ):
        code, stdout, stderr = _invoke_integration(["info"])
    assert code == 1
    assert json.loads(stdout)["error"]["code"] == "INTERNAL_ERROR"
    assert SECRET_SENTINEL not in stdout
    assert SECRET_SENTINEL not in stderr

    code, stdout, stderr = _invoke_integration([f"unknown-{SECRET_SENTINEL}"])
    assert code == 2
    assert SECRET_SENTINEL not in stdout
    assert SECRET_SENTINEL not in stderr


def test_f06_c04_xdg_tree_and_permission_spies_unchanged(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_xdg / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(isolated_xdg / "cache"))
    before = _tree_snapshot(isolated_xdg)

    bootstrap = patch(
        "ai_dev_loop.scheduler.infrastructure.sqlite_store.SqliteSchedulerStore.bootstrap"
    )
    chmod = patch("ai_dev_loop.paths.ensure_dir")
    subprocess_launch = patch("subprocess.Popen")

    with bootstrap as bootstrap_mock, chmod as chmod_mock, subprocess_launch as popen_mock:
        code, _stdout, _stderr = _invoke_integration(["info"])
    assert code == 0
    bootstrap_mock.assert_not_called()
    chmod_mock.assert_not_called()
    popen_mock.assert_not_called()
    assert _tree_snapshot(isolated_xdg) == before


def test_c05_packaged_schemas_exist() -> None:
    assert_packaged_schemas_exist()


def test_build_success_and_failure_envelopes_match_wire_shape() -> None:
    fixed = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    info = build_integration_info_data()
    success = build_success_envelope(info.model_dump(by_alias=True), observed_at=fixed)
    validate_envelope_wire_payload(success)
    assert success["ok"] is True
    failure = build_failure_envelope(
        IntegrationErrorCode.INVALID_ARGUMENT,
        "safe",
        observed_at=fixed,
    )
    validate_envelope_wire_payload(failure)
    assert failure["ok"] is False

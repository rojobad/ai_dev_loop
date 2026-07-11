"""Unit tests for CLI version and model-compatibility probes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_dev_loop.runners.probes import (
    CompatibilityClassification,
    probe_codex_model_compatibility,
    probe_cursor_model_compatibility,
    probe_version,
    run_tool_compatibility_probes,
)


def test_version_probes_capture_fake_cursor_and_codex_versions(
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_VERSION", "agent 9.8.7")
    monkeypatch.setenv("FAKE_CODEX_VERSION", "codex-cli 7.6.5")

    cursor = probe_version("agent", tool="cursor")
    codex = probe_version("codex", tool="codex")

    assert cursor.ok is True
    assert cursor.version == "agent 9.8.7"
    assert codex.ok is True
    assert codex.version == "codex-cli 7.6.5"


def test_codex_model_probe_classifies_compatible(
    fake_clis: dict[str, Path],
) -> None:
    fake_clis["codex_models_file"].write_text(
        json.dumps({"models": [{"id": "gpt-5.6-sol"}, {"id": "gpt-5.5"}]}),
        encoding="utf-8",
    )

    result = probe_codex_model_compatibility(
        "codex",
        required_model="gpt-5.6-sol",
        installed_version="codex-cli test",
    )

    assert result.classification == CompatibilityClassification.COMPATIBLE
    assert result.catalog_source == "debug_models"
    assert result.installed_version == "codex-cli test"


def test_codex_model_probe_classifies_missing_model_as_incompatible(
    fake_clis: dict[str, Path],
) -> None:
    fake_clis["codex_models_file"].write_text(
        json.dumps({"models": [{"id": "gpt-5.5"}]}),
        encoding="utf-8",
    )

    result = probe_codex_model_compatibility(
        "codex",
        required_model="gpt-5.6-sol",
    )

    assert result.classification == CompatibilityClassification.INCOMPATIBLE_MODEL
    assert "updating may add support" in result.detail
    assert "definitely" not in result.detail


def test_codex_model_probe_classifies_unrecognized_catalog_as_unknown(
    fake_clis: dict[str, Path],
) -> None:
    fake_clis["codex_models_file"].write_text(
        json.dumps({"unexpected": []}),
        encoding="utf-8",
    )

    result = probe_codex_model_compatibility(
        "codex",
        required_model="gpt-5.6-sol",
    )

    assert result.classification == CompatibilityClassification.UNKNOWN
    assert "unrecognized" in result.detail


def test_codex_model_probe_classifies_failed_refreshed_and_bundled_probes(
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODELS_FAIL", "1")

    result = probe_codex_model_compatibility(
        "codex",
        required_model="gpt-5.6-sol",
    )

    assert result.classification == CompatibilityClassification.PROBE_FAILED
    assert result.catalog_source == "debug_models_bundled"
    assert "catalog unavailable" in result.detail


def test_legacy_unset_codex_model_is_unknown_without_catalog_probe(
    fake_clis: dict[str, Path],
) -> None:
    result = probe_codex_model_compatibility("codex", required_model=None)

    assert result.classification == CompatibilityClassification.UNKNOWN
    assert "legacy Phase 9" in result.detail
    assert not fake_clis["codex_log"].exists()


@pytest.mark.parametrize(
    ("catalog", "required_model", "expected"),
    [
        ("composer-2.5-fast\n", "composer-2.5-fast", CompatibilityClassification.COMPATIBLE),
        (
            "composer-2.5-fast\n",
            "composer-9-missing",
            CompatibilityClassification.INCOMPATIBLE_MODEL,
        ),
    ],
)
def test_cursor_model_compatibility_uses_fake_models_catalog(
    fake_clis: dict[str, Path],
    catalog: str,
    required_model: str,
    expected: CompatibilityClassification,
) -> None:
    fake_clis["agent_models_file"].write_text(catalog, encoding="utf-8")

    result = probe_cursor_model_compatibility(
        "agent",
        required_model=required_model,
        installed_version="agent test",
    )

    assert result.classification == expected
    assert result.installed_version == "agent test"


def test_combined_probes_return_deterministic_cursor_then_codex_results(
    fake_clis: dict[str, Path],
) -> None:
    fake_clis["agent_models_file"].write_text("composer-2.5-fast\n", encoding="utf-8")
    fake_clis["codex_models_file"].write_text(
        json.dumps({"models": [{"id": "gpt-5.6-sol"}]}),
        encoding="utf-8",
    )

    compatibility, versions = run_tool_compatibility_probes(
        cursor_command="agent",
        cursor_model="composer-2.5-fast",
        codex_command="codex",
        codex_model="gpt-5.6-sol",
    )

    assert [item.tool for item in compatibility] == ["cursor", "codex"]
    assert all(
        item.classification == CompatibilityClassification.COMPATIBLE for item in compatibility
    )
    assert [item.tool for item in versions] == ["cursor", "codex"]

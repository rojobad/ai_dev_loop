"""Integration tests for Phase 21.1 local Integration API contract."""

from __future__ import annotations

import json
import subprocess
import venv as stdlib_venv
import zipfile
from pathlib import Path

import pytest
from tests.support.integration_offline_env import (
    OFFLINE_WHEELHOUSE,
    offline_subprocess_env,
    wheelhouse_ready,
)
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.integration_api.schemas import integration_schema_names

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_c05_plural_integrations_help_unchanged() -> None:
    result = runner.invoke(app, ["integrations", "--help"])
    assert result.exit_code == 0
    assert "install" in result.stdout
    assert "sessions" in result.stdout


def test_f02_singular_integration_help_via_clirunner() -> None:
    result = runner.invoke(app, ["integration", "--help"])
    assert result.exit_code == 0
    assert "info" in result.stdout
    info_help = runner.invoke(app, ["integration", "info", "--help"])
    assert info_help.exit_code == 0
    assert "--output" in info_help.stdout


@pytest.mark.skipif(
    not wheelhouse_ready(), reason="offline wheelhouse missing; run populate script"
)
def test_f07_offline_wheel_install_and_info(
    tmp_path: Path,
    isolated_xdg: Path,
    isolated_home: Path,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    install_venv = tmp_path / "venv"
    env = offline_subprocess_env(
        {
            "HOME": str(isolated_home),
            "XDG_STATE_HOME": str(isolated_xdg / "state"),
            "XDG_CONFIG_HOME": str(isolated_xdg / "config"),
            "XDG_CACHE_HOME": str(isolated_xdg / "cache"),
        }
    )

    stdlib_venv.EnvBuilder(with_pip=True).create(str(install_venv))
    python_bin = install_venv / "bin" / "python"
    subprocess.run(
        [
            str(python_bin),
            "-m",
            "pip",
            "install",
            "--no-index",
            f"--find-links={OFFLINE_WHEELHOUSE}",
            "hatchling",
            "build",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    subprocess.run(
        [
            str(python_bin),
            "-m",
            "build",
            "--outdir",
            str(dist),
            "--no-isolation",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    wheels = sorted(dist.glob("ai_dev_loop-*.whl"))
    assert wheels
    wheel = wheels[-1]
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        for schema_name in integration_schema_names():
            assert any(name.endswith(f"schemas/{schema_name}") for name in names), (
                f"missing schema {schema_name} in wheel"
            )

    subprocess.run(
        [
            str(python_bin),
            "-m",
            "pip",
            "install",
            "--no-index",
            f"--find-links={OFFLINE_WHEELHOUSE}",
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    proc = subprocess.run(
        [str(install_venv / "bin" / "ai_dev_loop"), "integration", "info", "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["apiVersion"] == {"major": 1, "minor": 5}

    schema_probe = subprocess.run(
        [
            str(python_bin),
            "-c",
            "from ai_dev_loop.integration_api.schemas import assert_packaged_schemas_exist; "
            "assert_packaged_schemas_exist(); print('ok')",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert schema_probe.returncode == 0
    assert schema_probe.stdout.strip() == "ok"

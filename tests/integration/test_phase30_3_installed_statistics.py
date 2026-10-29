"""Phase 30.3 installed base package reads a one-file SQLite restore."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from tests.integration.test_phase30_1_usage_ingestion import _complete_codex
from tests.support.phase30_3_restore_snapshot import (
    conceal_external_roots,
    recorded_external_roots,
    reveal_roots,
)

_IMPLEMENTER = {
    "input_tokens": 100,
    "cached_input_tokens": 60,
    "output_tokens": 10,
    "reasoning_output_tokens": 4,
}
_REVIEW = {"input_tokens": 25, "cached_input_tokens": 5, "output_tokens": 5}
_KNOWN_TOTAL = 140
_REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {"id": "gpt-5.6-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    root = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": root / "engine.sqlite3", "artifact_root": root / "artifacts"}


def test_a03_installed_base_wheel_reads_restored_statistics(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_id, _state = _complete_codex(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_usage=[_IMPLEMENTER],
        review_usage=[_REVIEW],
    )
    backup = tmp_path / "restored.sqlite3"
    _backup(scheduler_paths["db_path"], backup)
    distribution = tmp_path / "dist"
    distribution.mkdir()
    _build(distribution)
    wheel = _one(distribution, "ai_dev_loop-*.whl")
    sdist = _one(distribution, "ai_dev_loop-*.tar.gz")
    _assert_sdk_is_optional(wheel, sdist)
    interpreter = shutil.which("python3.11") or sys.executable
    venv = tmp_path / "venv"
    venv_python = _install(wheel, venv, interpreter)
    home = tmp_path / "restored-home"
    state_home = home / "state"
    database = state_home / "ai_dev_loop" / "engine.sqlite3"
    database.parent.mkdir(parents=True)
    shutil.copyfile(backup, database)
    assert not (database.parent / "artifacts").exists()
    unavailable = recorded_external_roots(scheduler_paths["db_path"])
    unavailable.append(scheduler_paths["artifact_root"])
    unavailable.append(scheduler_paths["db_path"].parent / "cursor-sdk")
    concealed, moved = conceal_external_roots(unavailable, checkout=_REPOSITORY)
    try:
        payload = _read_installed(
            venv_python,
            venv,
            home,
            state_home,
            run_id,
            tmp_path / "reader",
            unavailable_paths=concealed,
        )
    finally:
        reveal_roots(moved)
    assert Path(str(payload["python"])).resolve() == venv_python.resolve()
    assert payload["python311"] == ("available" if shutil.which("python3.11") else "unavailable")
    origin = Path(str(payload["origin"])).resolve()
    assert origin.is_relative_to(venv.resolve())
    assert not origin.is_relative_to(_REPOSITORY / "src")
    assert payload["cursor_sdk"] == "absent"
    assert payload["schema_version"] == 22
    assert payload["database_sha256_before"] == payload["database_sha256_after"]
    assert payload["opener_calls"] == 0
    assert payload["known_total_tokens"] == _KNOWN_TOTAL
    assert payload["coverage"] == "complete"
    assert payload["dashboard_known_total_tokens"] == _KNOWN_TOTAL
    assert payload["configured_models"] == ["gpt-5.5", "gpt-5.6-sol"]
    assert payload["witness"] == ""
    assert payload["forbidden_env"] == []
    assert payload["unavailable_present"] == []
    assert set(payload["unavailable_missing"]) == {str(path) for path in concealed}
    assert payload["review_available"] is False
    assert payload["review_reason"] == "not_recorded"
    assert payload["review_content"] in {None, ""}
    assert (_REPOSITORY / "src" / "ai_dev_loop" / "__init__.py").is_file()
    assert not (database.parent / "artifacts").exists()


def _backup(source: Path, dest: Path) -> None:
    live = sqlite3.connect(source)
    try:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target = sqlite3.connect(dest)
        try:
            live.backup(target)
        finally:
            target.close()
    finally:
        live.close()


def _build(distribution: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(distribution)],
        cwd=_REPOSITORY,
        env=_tool_env(),
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _install(wheel: Path, venv: Path, interpreter: str) -> Path:
    uv = shutil.which("uv")
    assert uv is not None
    subprocess.run(
        [uv, "venv", str(venv), "--python", interpreter],
        cwd=venv.parent,
        env=_tool_env(),
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    python = venv / "bin" / "python"
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), str(wheel)],
        cwd=venv.parent,
        env=_tool_env(),
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return python


def _read_installed(
    interpreter: Path,
    install_root: Path,
    home: Path,
    state_home: Path,
    run_id: str,
    work: Path,
    *,
    unavailable_paths: list[Path],
) -> dict[str, object]:
    work.mkdir()
    script = work / "read_restored.py"
    script.write_text(_READER, encoding="utf-8")
    trap = work / "trap"
    trap.mkdir()
    witness = work / "provider-witness"
    witness.write_text("", encoding="utf-8")
    launcher = '#!/bin/sh\nprintf \'%s\\n\' "$0" >> "$PROVIDER_WITNESS"\nexit 99\n'
    for name in ("codex", "agent", "cursor"):
        path = trap / name
        path.write_text(launcher, encoding="utf-8")
        path.chmod(0o755)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(work / "sentinel-codex-home")
    env["OPENAI_API_KEY"] = "sk-phase303-sentinel"
    env["OPENAI_BASE_URL"] = "http://127.0.0.1:9"
    for key in list(env):
        if key.startswith(
            ("FAKE_", "CURSOR_", "PYTHONPATH", "OPENAI_", "CODEX_", "ANTHROPIC_", "AZURE_OPENAI_")
        ):
            env.pop(key, None)
    env.update(
        {
            "PATH": f"{trap}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "PROVIDER_WITNESS": str(witness),
            "HOME": str(home),
            "XDG_STATE_HOME": str(state_home),
            "XDG_CONFIG_HOME": str(work / "config"),
            "XDG_CACHE_HOME": str(work / "cache"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "RESTORED_RUN_ID": run_id,
            "INSTALL_ROOT": str(install_root),
            "UNAVAILABLE_PATHS": json.dumps([str(path) for path in unavailable_paths]),
            "TMPDIR": "/tmp",
            "TMP": "/tmp",
            "TEMP": "/tmp",
        }
    )
    completed = subprocess.run(
        [str(interpreter), str(script)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    payload["witness"] = witness.read_text(encoding="utf-8")
    return payload


def _assert_sdk_is_optional(wheel: Path, sdist: Path) -> None:
    wheel_metadata = _wheel_metadata(wheel)
    sdist_metadata = _sdist_metadata(sdist)
    for metadata in (wheel_metadata, sdist_metadata):
        assert "Requires-Python: >=3.11" in metadata
        hard = [
            line
            for line in metadata.splitlines()
            if line.startswith("Requires-Dist:") and "cursor-sdk" in line and "extra ==" not in line
        ]
        assert hard == []
        assert any("cursor-sdk" in line and "extra ==" in line for line in metadata.splitlines())


def _wheel_metadata(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        name = next(item for item in archive.namelist() if item.endswith(".dist-info/METADATA"))
        return archive.read(name).decode("utf-8")


def _sdist_metadata(sdist: Path) -> str:
    with tarfile.open(sdist) as archive:
        member = next(item for item in archive.getmembers() if item.name.endswith("/PKG-INFO"))
        extracted = archive.extractfile(member)
        assert extracted is not None
        return extracted.read().decode("utf-8")


def _one(distribution: Path, pattern: str) -> Path:
    matches = list(distribution.glob(pattern))
    assert len(matches) == 1
    return matches[0]


def _tool_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "TMPDIR": "/tmp",
            "TMP": "/tmp",
            "TEMP": "/tmp",
            "UV_CACHE_DIR": "/tmp/phase303-uv-cache",
        }
    )
    return env


_READER = r"""
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import ai_dev_loop
import ai_dev_loop.scheduler.application.token_usage_query as query_module
from ai_dev_loop.cli import app
from ai_dev_loop.dashboard.adapters.http import (
    DashboardHttpDispatcher,
    default_http_service_factories,
)
from ai_dev_loop.scheduler.infrastructure.readonly_protected_artifacts import (
    ReadOnlyProtectedArtifactStore,
)
from typer.testing import CliRunner

origin = str(Path(ai_dev_loop.__file__).resolve())
install_root = str(Path(os.environ["INSTALL_ROOT"]).resolve())
if not origin.startswith(install_root + os.sep):
    raise SystemExit(f"import origin {origin} is outside {install_root}")
try:
    import cursor_sdk  # noqa: F401
except ImportError:
    sdk = "absent"
else:
    raise SystemExit("cursor_sdk is installed in the base environment")

state_home = Path(os.environ["XDG_STATE_HOME"])
database = state_home / "ai_dev_loop" / "engine.sqlite3"
artifact_root = state_home / "ai_dev_loop" / "artifacts"

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def user_version(path: Path) -> int:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        row = conn.execute("PRAGMA user_version").fetchone()
    if row is None:
        raise SystemExit("missing user_version")
    return int(row[0])

before = digest(database)
version = user_version(database)
calls = 0

def refuse(*_args: object, **_kwargs: object) -> None:
    global calls
    calls += 1
    raise AssertionError("installed statistics opened a usage source")

original_auth = query_module.authenticate_process_attempt
original_read = ReadOnlyProtectedArtifactStore.read_verified_bytes
query_module.authenticate_process_attempt = refuse
ReadOnlyProtectedArtifactStore.read_verified_bytes = refuse
forbidden = sorted(
    key
    for key in os.environ
    if key.startswith(("FAKE_", "CURSOR_", "OPENAI_", "CODEX_", "ANTHROPIC_", "AZURE_OPENAI_"))
)
unavailable = [Path(item) for item in json.loads(os.environ["UNAVAILABLE_PATHS"])]
if not unavailable or forbidden or any(path.exists() for path in unavailable):
    present = [str(path) for path in unavailable if path.exists()]
    raise SystemExit(f"installed read is not isolated: env={forbidden} paths={present}")
run_id = os.environ["RESTORED_RUN_ID"]
completed = CliRunner().invoke(app, ["scheduler", "usage", run_id, "--output", "json"])
if completed.exit_code != 0:
    raise SystemExit(completed.stdout + completed.stderr)
usage = json.loads(completed.stdout)
http = DashboardHttpDispatcher(
    default_http_service_factories(db_path=database, artifact_root=artifact_root)
).dispatch(
    "dashboard.token-usage",
    {
        "fromUtc": "2026-10-01T00:00:00Z",
        "toUtc": "2026-11-01T00:00:00Z",
        "timeZone": "UTC",
        "runId": run_id,
    },
)
models = sorted({row["configuredModel"] for row in http["rows"]})
query_module.authenticate_process_attempt = original_auth
ReadOnlyProtectedArtifactStore.read_verified_bytes = original_read
uri = f"file:{database.resolve().as_posix()}?mode=ro"
with sqlite3.connect(uri, uri=True) as conn:
    review_row = conn.execute(
        "SELECT attempt.attempt_id "
        "FROM scheduler_attempts AS attempt "
        "JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id "
        "WHERE attempt.run_id = ? AND effect.effect_kind = 'codex.bootstrap_review' "
        "ORDER BY attempt.rowid LIMIT 1",
        (run_id,),
    ).fetchone()
if review_row is None:
    raise SystemExit("no review attempt")
review = CliRunner().invoke(
    app,
    [
        "integration",
        "run",
        "review-content",
        run_id,
        "--attempt",
        str(review_row[0]),
        "--kind",
        "prompt",
    ],
)
if review.exit_code != 0:
    raise SystemExit(review.stdout + review.stderr)
review_data = json.loads(review.stdout)["data"]
sys.stdout.write(json.dumps({
    "python": sys.executable,
    "python311": "available" if sys.version_info[:2] == (3, 11) else "unavailable",
    "origin": origin,
    "cursor_sdk": sdk,
    "schema_version": version,
    "database_sha256_before": before,
    "database_sha256_after": digest(database),
    "opener_calls": calls,
    "known_total_tokens": usage["direct"]["known_total_tokens"],
    "coverage": usage["direct"]["coverage"],
    "dashboard_known_total_tokens": http["source"]["knownTotalTokens"],
    "configured_models": models,
    "forbidden_env": forbidden,
    "unavailable_missing": [str(path) for path in unavailable if not path.exists()],
    "unavailable_present": [str(path) for path in unavailable if path.exists()],
    "review_available": review_data["available"],
    "review_reason": review_data["reason"],
    "review_content": review_data.get("contentBase64"),
}))
"""

"""Phase 26.7 installed package, harness lifecycle, and cleanup acceptance."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.hatch_build import validate_phase26_build_inputs

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPOSITORY_ROOT / "scripts/verify-phase26-dashboard.py"


def _environment(tmp_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
            "UV_LINK_MODE": "copy",
            "TMPDIR": "/tmp",
            "TMP": "/tmp",
            "TEMP": "/tmp",
        }
    )
    return environment


def _python_version(path: Path) -> tuple[int, int, int] | None:
    result = subprocess.run(
        [
            str(path),
            "-I",
            "-c",
            "import json,sys; print(json.dumps(list(sys.version_info[:3])))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    values = json.loads(result.stdout)
    if not isinstance(values, list) or len(values) != 3:
        return None
    return tuple(int(value) for value in values)  # type: ignore[return-value]


def _required_python(kind: str) -> Path:
    environment_name = "PHASE26_PYTHON_311" if kind == "3.11" else "PHASE26_PYTHON_NEWER"
    candidates: list[str] = []
    explicit = os.environ.get(environment_name)
    if explicit:
        candidates.append(explicit)
    if kind == "3.11":
        candidates.extend(("python3.11",))
    else:
        candidates.extend(("python3.13", "python3.12", sys.executable))
    for candidate in candidates:
        resolved = (
            Path(candidate).expanduser().resolve()
            if os.sep in candidate
            else Path(shutil.which(candidate) or "")
        )
        if not resolved.is_file():
            continue
        version = _python_version(resolved)
        if version is None:
            continue
        if kind == "3.11" and version[:2] == (3, 11):
            return resolved
        if kind == "newer" and version[:2] > (3, 11):
            return resolved
    raise AssertionError(
        f"Phase 26.7 requires a {kind} interpreter; set {environment_name} to an executable"
    )


@pytest.fixture(scope="module", params=("3.11", "newer"))
def installed_python(request: pytest.FixtureRequest) -> Path:
    return _required_python(str(request.param))


def _harness(
    tmp_path: Path,
    *args: str,
    timeout: float = 180,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), *args],
        cwd=REPOSITORY_ROOT,
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _defer_when_node_tooling_is_unavailable(result: subprocess.CompletedProcess[str]) -> None:
    marker = "BLOCKED_CONTRACT: Node tooling is unavailable"
    if result.returncode != 0 and marker in result.stderr:
        pytest.skip(
            "D-02 deferred by plan owner 2026-10-18: installed Dashboard/browser "
            "acceptance requires unavailable Node tooling"
        )


def test_c267_1_and_c267_3_installed_acceptance_real_server_browser_and_package(
    hermetic_tmp_path: Path,
    installed_python: Path,
) -> None:
    result = _harness(
        hermetic_tmp_path,
        "accept",
        "--python",
        str(installed_python),
        "--case",
        "api",
        "--case",
        "browser",
        "--case",
        "safety",
    )
    _defer_when_node_tooling_is_unavailable(result)
    assert result.returncode == 0, result.stderr
    assert "PASS packaging wheel=" in result.stdout
    selected_version = _python_version(installed_python)
    assert selected_version is not None
    assert f"python={'.'.join(str(item) for item in selected_version)}" in result.stdout
    assert "PASS api runs=27 reviews=3 timeline=26" in result.stdout
    assert "PASS browser installed offline journey=1" in result.stdout
    assert "PASS safety authority=unchanged runtime=loopback/no-node" in result.stdout
    assert "READY_FOR_REVIEW" in result.stdout


def test_c267_2_list_prepare_usable_and_exact_cleanup(hermetic_tmp_path: Path) -> None:
    inventory = _harness(hermetic_tmp_path, "--list")
    assert inventory.returncode == 0, inventory.stderr
    assert [line.split("\t", 1)[0] for line in inventory.stdout.splitlines()] == [
        "packaging",
        "api",
        "browser",
        "safety",
    ]

    registry = hermetic_tmp_path / "prepared.json"
    prepared = _harness(
        hermetic_tmp_path,
        "prepare",
        "--python",
        str(_required_python("3.11")),
        "--registry",
        str(registry),
    )
    _defer_when_node_tooling_is_unavailable(prepared)
    assert prepared.returncode == 0, prepared.stderr
    assert "MANUAL_PENDING" in prepared.stdout
    record = json.loads(registry.read_text(encoding="utf-8"))
    root = Path(record["resourceRoot"])
    assert root.is_dir()
    assert (root / "fixture.json").is_file()

    connection = http.client.HTTPConnection("127.0.0.1", int(record["port"]), timeout=5)
    try:
        connection.request(
            "GET",
            "/",
            headers={"Host": f"127.0.0.1:{record['port']}"},
        )
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    assert response.status == 200
    assert b"/assets/dashboard.js" in body

    cleaned = _harness(
        hermetic_tmp_path,
        "cleanup",
        "--registry",
        str(registry),
    )
    assert cleaned.returncode == 0, cleaned.stderr
    assert "CLEANED" in cleaned.stdout
    assert not registry.exists()
    assert not root.exists()


def test_c267_3_environment_preserves_caller_tooling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHASE26_NODE_BIN", "/owned/alternate/node-bin")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/owned/alternate/browsers")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/owned/alternate/libraries")
    environment = _environment(tmp_path)
    assert environment["PHASE26_NODE_BIN"] == "/owned/alternate/node-bin"
    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == "/owned/alternate/browsers"
    assert environment["LD_LIBRARY_PATH"] == "/owned/alternate/libraries"


@pytest.mark.parametrize("conflict", ["token", "process", "symlink"])
def test_c267_2_cleanup_preserves_foreign_or_uncertain_resources(
    hermetic_tmp_path: Path,
    conflict: str,
) -> None:
    root = Path(f"/tmp/ai-dev-loop-phase26-dashboard-foreign-{os.getpid()}-{conflict}")
    root.mkdir(mode=0o700)
    registry = hermetic_tmp_path / f"foreign-{conflict}.json"
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "status": "manual_pending",
        "resourceRoot": str(root),
        "ownerUid": os.getuid(),
        "ownerToken": "a" * 48,
        "pid": os.getpid(),
        "processStart": "0" if conflict == "process" else "unknown",
        "port": 65534,
        "executable": sys.executable,
        "fixture": str(root / "fixture.json"),
        "createdAt": 0.0,
    }
    owner = dict(payload)
    if conflict == "token":
        owner["ownerToken"] = "b" * 48
    registry.write_text(json.dumps(payload), encoding="utf-8")
    owner_path = root / ".owner.json"
    owner_path.write_text(json.dumps(owner), encoding="utf-8")
    sentinel = root / "preserve.txt"
    sentinel.write_text("foreign\n", encoding="utf-8")
    if conflict == "symlink":
        linked = root.with_name(f"{root.name}-link")
        linked.symlink_to(root, target_is_directory=True)
        payload["resourceRoot"] = str(linked)
        owner["resourceRoot"] = str(linked)
        registry.write_text(json.dumps(payload), encoding="utf-8")
        owner_path.write_text(json.dumps(owner), encoding="utf-8")
    try:
        result = _harness(
            hermetic_tmp_path,
            "cleanup",
            "--registry",
            str(registry),
        )
        assert result.returncode == 1
        assert "resources were preserved" in result.stderr
        assert sentinel.read_text(encoding="utf-8") == "foreign\n"
        assert registry.is_file()
    finally:
        if conflict == "symlink":
            linked.unlink(missing_ok=True)
        shutil.rmtree(root)
        registry.unlink(missing_ok=True)


def test_c267_3_build_guard_rejects_stale_frontend_sources(tmp_path: Path) -> None:
    copy_root = tmp_path / "copy"
    shutil.copytree(
        REPOSITORY_ROOT / "dashboard-web",
        copy_root / "dashboard-web",
        ignore=shutil.ignore_patterns("node_modules", "test-results"),
    )
    shutil.copytree(
        REPOSITORY_ROOT / "src/ai_dev_loop/dashboard/web_dist",
        copy_root / "src/ai_dev_loop/dashboard/web_dist",
    )
    shutil.copytree(
        REPOSITORY_ROOT / "src/ai_dev_loop/schemas",
        copy_root / "src/ai_dev_loop/schemas",
    )
    validate_phase26_build_inputs(copy_root)
    source = copy_root / "dashboard-web/src/main.tsx"
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing or stale"):
        validate_phase26_build_inputs(copy_root)


def test_c267_3_docs_and_handoff_match_public_contract() -> None:
    operator_doc = (REPOSITORY_ROOT / "docs/operacion/dashboard-local.md").read_text(
        encoding="utf-8"
    )
    reference_doc = (REPOSITORY_ROOT / "docs/referencia/dashboard.md").read_text(encoding="utf-8")
    walkthrough = (
        REPOSITORY_ROOT / "archive/implementation-history/plans/phase-26-manual-walkthrough.md"
    ).read_text(encoding="utf-8")
    handoff = (
        REPOSITORY_ROOT
        / "archive/implementation-history/plans/phase-26-to-hub-dashboard-handoff.md"
    ).read_text(encoding="utf-8")

    assert "ai_dev_loop dashboard serve --port 8765" in operator_doc
    assert "Pause automatic refresh" in operator_doc
    assert "memoria" in operator_doc
    assert "--python /ruta/a/python3.11" in operator_doc
    assert "PHASE26_NODE_BIN" in operator_doc
    historical_tooling_marker = "phase" + "263"
    assert historical_tooling_marker not in operator_doc
    assert "API de integración actual es 1.12" in reference_doc
    assert "SQLite\nschema 17" in reference_doc
    assert "codex.implementer_resume" in reference_doc
    assert "MANUAL_PENDING" in walkthrough
    assert "fixture-project" in walkthrough
    assert "--python /ruta/a/python3.11" in walkthrough
    assert "14,400" in walkthrough
    assert "cleanup" in walkthrough
    assert "Integration API **1.9**" in handoff
    assert "dashboard=true" in handoff
    assert "Additional future work — not implemented" in handoff
    assert "Nothing in this handoff implements or authorizes Hub" in handoff

    schema_root = REPOSITORY_ROOT / "src/ai_dev_loop/schemas"
    for schema in (
        "integration-api-dashboard-shared-v1.json",
        "integration-api-dashboard-projects-data-v1.json",
        "integration-api-dashboard-summary-data-v1.json",
        "integration-api-dashboard-runs-data-v1.json",
        "integration-api-dashboard-activity-data-v1.json",
    ):
        assert schema in handoff
        assert (schema_root / schema).is_file()

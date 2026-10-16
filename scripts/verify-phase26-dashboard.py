#!/usr/bin/env python3
"""Installed, loopback-only Phase 26 dashboard acceptance and prepared-site owner."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.hatch_build import validate_phase26_build_inputs  # noqa: E402
from tests.support.phase26_installed_fixture import (  # noqa: E402
    InstalledDashboardFixture,
    build_installed_dashboard_fixture,
)

CASES = {
    "packaging": "frontend build, wheel/sdist inventory, isolated install and import origin",
    "api": "installed CLI/server, typed API, provider roles, paging and deliberate content",
    "browser": "real installed offline browser journey across overview, map, timeline and content",
    "safety": "read-only authority snapshot, local-only runtime and owned cleanup metadata",
}
REGISTRY_SCHEMA = 1
RESOURCE_PREFIX = "ai-dev-loop-phase26-dashboard-"
START_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class BuiltPackage:
    wheel: Path
    sdist: Path
    venv: Path
    python: Path
    cli: Path
    python_version: tuple[int, int, int]


@dataclass
class InstalledSite:
    root: Path
    package: BuiltPackage
    fixture: InstalledDashboardFixture
    port: int
    process: subprocess.Popen[bytes]
    environment: dict[str, str]
    authority_snapshot: dict[str, str]


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 300.0,
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args[:3])}: {detail}")
    return result, elapsed


def _node_bin() -> Path:
    explicit = os.environ.get("PHASE26_NODE_BIN")
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if all((candidate / name).is_file() for name in ("node", "npm", "npx")):
            return candidate
        raise RuntimeError("PHASE26_NODE_BIN does not contain node, npm and npx")
    node = shutil.which("node")
    npm = shutil.which("npm")
    npx = shutil.which("npx")
    if node and npm and npx:
        directories = {Path(item).parent.resolve() for item in (node, npm, npx)}
        if len(directories) == 1:
            return directories.pop()
    raise RuntimeError(
        "Node tooling is unavailable; put node/npm/npx on PATH or set PHASE26_NODE_BIN"
    )


def _python_identity(interpreter: Path, *, cwd: Path) -> tuple[int, int, int]:
    candidate = interpreter.expanduser().resolve()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise RuntimeError(f"selected Python interpreter is not executable: {candidate}")
    result, _ = _run(
        [
            str(candidate),
            "-I",
            "-c",
            "import json,sys; print(json.dumps(list(sys.version_info[:3])))",
        ],
        cwd=cwd,
    )
    try:
        version = tuple(json.loads(result.stdout))
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("selected Python interpreter returned invalid version data") from exc
    if len(version) != 3 or not all(type(item) is int for item in version):
        raise RuntimeError("selected Python interpreter returned invalid version data")
    typed_version = cast(tuple[int, int, int], version)
    if typed_version < (3, 11, 0):
        raise RuntimeError("installed dashboard acceptance requires Python >=3.11")
    return typed_version


def _build_environment(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "TMPDIR": "/tmp",
            "TMP": "/tmp",
            "TEMP": "/tmp",
            "UV_CACHE_DIR": str(root / "uv-cache"),
            "npm_config_cache": str(root / "npm-cache"),
        }
    )
    node_bin = _node_bin()
    environment["PATH"] = f"{node_bin}{os.pathsep}{environment.get('PATH', '')}"
    return environment


def _inspect_distributions(wheel: Path, sdist: Path) -> None:
    required_wheel = {
        "ai_dev_loop/dashboard/web_dist/build-manifest.json",
        "ai_dev_loop/dashboard/web_dist/index.html",
        "ai_dev_loop/dashboard/web_dist/assets/dashboard.css",
        "ai_dev_loop/dashboard/web_dist/assets/dashboard.js",
        "ai_dev_loop/schemas/integration-api-dashboard-activity-data-v1.json",
        "ai_dev_loop/schemas/integration-api-dashboard-projects-data-v1.json",
        "ai_dev_loop/schemas/integration-api-dashboard-runs-data-v1.json",
        "ai_dev_loop/schemas/integration-api-dashboard-shared-v1.json",
        "ai_dev_loop/schemas/integration-api-dashboard-summary-data-v1.json",
    }
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        missing = required_wheel - names
        if missing:
            raise RuntimeError(f"wheel is missing dashboard package members: {sorted(missing)}")
        if any(name.endswith(".map") or "node_modules/" in name for name in names):
            raise RuntimeError("wheel contains forbidden frontend build material")
        manifest = json.loads(archive.read("ai_dev_loop/dashboard/web_dist/build-manifest.json"))
        if manifest.get("schemaVersion") != 1:
            raise RuntimeError("wheel dashboard build manifest is invalid")

    with tarfile.open(sdist, "r:gz") as archive:
        names = {member.name for member in archive.getmembers() if member.isfile()}
    required_suffixes = {
        "dashboard-web/package-lock.json",
        "dashboard-web/package.json",
        "dashboard-web/playwright.config.ts",
        "dashboard-web/playwright.installed.config.ts",
        "dashboard-web/scripts/write-build-manifest.mjs",
        "dashboard-web/src/main.tsx",
        "dashboard-web/vite.config.ts",
        "scripts/hatch_build.py",
        "scripts/verify-phase26-dashboard.py",
    }
    for suffix in required_suffixes:
        if not any(name.endswith(suffix) for name in names):
            raise RuntimeError(f"sdist is missing required source: {suffix}")
    forbidden = (
        "node_modules/",
        "test-results/",
        "/tests/fixtures/",
        "engine.sqlite3",
        "/artifacts/runs/",
        ".env",
    )
    if any(any(token in name for token in forbidden) or name.endswith(".map") for name in names):
        raise RuntimeError("sdist contains runtime/test state or forbidden generated material")


def _build_and_install(root: Path, *, interpreter: Path) -> BuiltPackage:
    environment = _build_environment(root)
    selected_python = interpreter.expanduser().resolve()
    selected_version = _python_identity(selected_python, cwd=root)
    _run(
        [str(_node_bin() / "npm"), "run", "build"],
        cwd=REPOSITORY_ROOT / "dashboard-web",
        env=environment,
    )
    validate_phase26_build_inputs(REPOSITORY_ROOT)
    distribution = root / "dist"
    distribution.mkdir()
    _run(
        ["uv", "build", "--out-dir", str(distribution)],
        cwd=REPOSITORY_ROOT,
        env=environment,
    )
    wheels = sorted(distribution.glob("*.whl"))
    sdists = sorted(distribution.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError("package build did not produce exactly one wheel and one sdist")
    wheel, sdist = wheels[0], sdists[0]
    _inspect_distributions(wheel, sdist)

    venv = root / "installed-venv"
    _run(
        ["uv", "venv", "--python", str(selected_python), str(venv)],
        cwd=root,
        env=environment,
    )
    python = venv / "bin/python"
    cli = venv / "bin/ai_dev_loop"
    _run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        cwd=root,
        env=environment,
    )
    runtime = root / "runtime"
    runtime.mkdir()
    isolated, _ = _run(
        [
            str(python),
            "-I",
            "-c",
            "import json,pathlib,sys,ai_dev_loop; print(json.dumps(list(sys.version_info[:3]))); print(pathlib.Path(ai_dev_loop.__file__).resolve())",
        ],
        cwd=runtime,
        env={key: value for key, value in environment.items() if key != "PYTHONPATH"},
    )
    lines = [line.strip() for line in isolated.stdout.splitlines() if line.strip()]
    try:
        installed_version = tuple(json.loads(lines[0]))
    except (IndexError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("installed acceptance returned invalid Python version data") from exc
    if installed_version != selected_version:
        raise RuntimeError("installed acceptance did not use the explicitly selected Python")
    module_path = Path(lines[-1]).resolve()
    if (
        venv.resolve() not in module_path.parents
        or REPOSITORY_ROOT.resolve() in module_path.parents
    ):
        raise RuntimeError(
            "installed package import resolved outside the isolated wheel environment"
        )
    if not cli.is_file():
        raise RuntimeError("wheel installation did not create the ai_dev_loop entry point")
    return BuiltPackage(
        wheel=wheel,
        sdist=sdist,
        venv=venv,
        python=python,
        cli=cli,
        python_version=selected_version,
    )


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _authority_snapshot(fixture: InstalledDashboardFixture) -> dict[str, str]:
    snapshot = {"engine.sqlite3": _hash_file(fixture.db_path)}
    for path in sorted(fixture.artifact_root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            snapshot[f"artifacts/{path.relative_to(fixture.artifact_root).as_posix()}"] = (
                _hash_file(path)
            )
    return snapshot


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _server_environment(
    root: Path,
    package: BuiltPackage,
    fixture: InstalledDashboardFixture,
) -> dict[str, str]:
    environment = os.environ.copy()
    runtime_path = os.pathsep.join(
        [str(package.venv / "bin"), str(fixture.provider_bin), "/usr/bin", "/bin"]
    )
    environment.update(
        {
            "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "xdg/config"),
            "XDG_STATE_HOME": str(fixture.state_home),
            "XDG_CACHE_HOME": str(root / "xdg/cache"),
            "PATH": runtime_path,
            "PYTHONNOUSERSITE": "1",
            "FAKE_CODEX_CAPACITY": "available",
            "FAKE_CODEX_MODELS_FILE": str(root / "providers/codex_models.json"),
        }
    )
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    if shutil.which("node", path=runtime_path) is not None:
        raise RuntimeError("installed server runtime PATH unexpectedly contains Node")
    return environment


def _http_get(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _http_read(port: int, operation: str, parameters: dict[str, object]) -> dict[str, Any]:
    body = json.dumps({"operation": operation, "parameters": parameters}).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(
            "POST",
            "/api/dashboard/read",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Host": f"127.0.0.1:{port}",
                "Origin": f"http://127.0.0.1:{port}",
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
    finally:
        connection.close()
    if response.status != 200 or payload.get("ok") is not True:
        raise RuntimeError(
            f"installed HTTP read failed for {operation}: {response.status} {payload.get('error')}"
        )
    return payload["data"]


def _start_site(root: Path, package: BuiltPackage) -> InstalledSite:
    fixture = build_installed_dashboard_fixture(root)
    snapshot = _authority_snapshot(fixture)
    environment = _server_environment(root, package, fixture)
    port = _reserve_port()
    runtime = root / "runtime"
    stdout = (root / "server.stdout.log").open("wb")
    stderr = (root / "server.stderr.log").open("wb")
    try:
        process = subprocess.Popen(
            [str(package.cli), "dashboard", "serve", "--port", str(port)],
            cwd=runtime,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    finally:
        stdout.close()
        stderr.close()
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"installed dashboard server exited early with {process.returncode}")
        try:
            status, _, body = _http_get(port, "/")
            if status == 200 and b"/assets/dashboard.js" in body:
                break
        except OSError:
            pass
        time.sleep(0.05)
    else:
        _stop_process(process)
        raise RuntimeError("installed dashboard server did not become ready")
    return InstalledSite(
        root=root,
        package=package,
        fixture=fixture,
        port=port,
        process=process,
        environment=environment,
        authority_snapshot=snapshot,
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def _assert_api(site: InstalledSite) -> None:
    fixture = site.fixture
    common: dict[str, object] = {
        "fromUtc": "2026-10-14T00:00:00Z",
        "toUtc": "2026-10-17T00:00:00Z",
        "projectName": "fixture-project",
        "timeZone": "UTC",
    }
    info, _ = _run(
        [str(site.package.cli), "integration", "info", "--output", "json"],
        cwd=site.root / "runtime",
        env=site.environment,
    )
    info_payload = json.loads(info.stdout)
    if info_payload["apiVersion"] != {"major": 1, "minor": 9}:
        raise RuntimeError("installed Integration API version is not 1.9")
    if info_payload["data"]["capabilities"]["dashboard"] is not True:
        raise RuntimeError("installed dashboard capability is not registered")

    summary = _http_read(site.port, "dashboard.summary", common)
    if summary["totals"]["runsStarted"] != fixture.runs_started:
        raise RuntimeError("installed summary started-run total disagrees with fixture")
    runs = _http_read(site.port, "dashboard.runs", {**common, "offset": 0, "limit": 100})
    if runs["totalCount"] != fixture.map_total:
        raise RuntimeError("installed map total disagrees with fixture")
    if {item["implementerProvider"] for item in runs["items"]} != {"cursor", "codex"}:
        raise RuntimeError("installed map omitted an implementer provider")
    replacement = next(
        item for item in runs["items"] if item["runId"] == fixture.replacement_run_id
    )
    binding = replacement["sequenceBinding"]
    if (
        binding["attemptKind"] != "implementer_retry"
        or binding["sourceRunId"] != fixture.source_run_id
    ):
        raise RuntimeError("installed map did not preserve authoritative recovery lineage")

    expected_activity = fixture.activity_expectations
    for metric in ("submissions", "review-rounds", "process-hours"):
        activity = _http_read(site.port, "dashboard.activity", {**common, "metric": metric})
        expected = expected_activity[metric]
        if (
            activity["unit"] != expected["unit"]
            or activity["total"] != expected["total"]
            or activity["days"] != expected["days"]
        ):
            raise RuntimeError(f"installed {metric} totals/day buckets disagree with fixture")
        if metric == "process-hours" and (
            activity["coverage"]["missingDurationAttempts"] != 1
            or activity["coverage"]["warnings"] != ["missing_duration_attempts"]
        ):
            raise RuntimeError("installed active-attempt process coverage disagrees with fixture")

    timeline_first = _http_read(
        site.port,
        "run.timeline",
        {"runId": fixture.replacement_run_id, "offset": 0, "limit": 25},
    )
    timeline_second = _http_read(
        site.port,
        "run.timeline",
        {"runId": fixture.replacement_run_id, "offset": 25, "limit": 25},
    )
    inspection = _http_read(
        site.port,
        "run.inspect",
        {"runId": fixture.replacement_run_id},
    )
    if inspection["attemptCount"] != fixture.timeline_total:
        raise RuntimeError("installed timeline total disagrees with fixture")
    if not timeline_first["page"]["hasMore"] or not timeline_second["items"]:
        raise RuntimeError("installed timeline did not exercise a second explicit page")

    initial = _http_read(
        site.port,
        "run.initial-prompt",
        {"runId": fixture.replacement_run_id, "byteOffset": 0, "limit": 65_536},
    )
    if not initial["available"] or initial["returnedBytes"] <= 0:
        raise RuntimeError("installed initial prompt context is unavailable")
    historical = _http_read(
        site.port,
        "run.review",
        {
            "runId": fixture.replacement_run_id,
            "attemptId": fixture.historical_review_attempt_id,
        },
    )
    if historical["content"]["prompt"] != {"available": False, "reason": "not_recorded"}:
        raise RuntimeError("installed historical prompt absence was not explicit")
    exact = _http_read(
        site.port,
        "run.review-content",
        {
            "runId": fixture.replacement_run_id,
            "attemptId": fixture.exact_review_attempt_id,
            "kind": "prompt",
            "byteOffset": 0,
            "limit": 65_536,
        },
    )
    if not exact["available"] or exact["returnedBytes"] <= 0:
        raise RuntimeError("installed exact reviewer prompt is unavailable")
    for stream in ("stdout", "stderr"):
        output = _http_read(
            site.port,
            "run.output",
            {
                "runId": fixture.replacement_run_id,
                "attemptId": fixture.implementer_attempt_id,
                "stream": stream,
                "byteOffset": 0,
                "limit": 65_536,
            },
        )
        if not output["available"] or output["component"] != "codex_implementer":
            raise RuntimeError(f"installed Codex implementer {stream} ownership is invalid")
    active = _http_read(
        site.port,
        "run.output",
        {
            "runId": fixture.source_run_id,
            "attemptId": fixture.active_attempt_id,
            "stream": "stdout",
            "byteOffset": 0,
            "limit": 65_536,
        },
    )
    if active["complete"] is not False or active["nextOffset"] is None:
        raise RuntimeError("installed active output did not preserve pollable EOF semantics")

    connection = http.client.HTTPConnection("127.0.0.1", site.port, timeout=5)
    body = json.dumps(
        {
            "operation": "run.review-content",
            "parameters": {
                "runId": fixture.replacement_run_id,
                "attemptId": fixture.implementer_attempt_id,
                "kind": "prompt",
            },
        }
    ).encode()
    try:
        connection.request(
            "POST",
            "/api/dashboard/read",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{site.port}",
                "Host": f"127.0.0.1:{site.port}",
            },
        )
        rejected = connection.getresponse()
        rejected_payload = json.loads(rejected.read())
    finally:
        connection.close()
    if rejected.status != 400 or rejected_payload["error"]["code"] != "INVALID_ARGUMENT":
        raise RuntimeError("installed reviewer-content accepted a Codex implementer attempt")


def _assert_browser(site: InstalledSite, root: Path) -> None:
    environment = _build_environment(root)
    environment.update(
        {
            "PHASE26_DASHBOARD_BASE_URL": f"http://127.0.0.1:{site.port}",
            "PHASE26_DASHBOARD_FIXTURE": str(root / "fixture.json"),
        }
    )
    _run(
        [
            str(_node_bin() / "npx"),
            "playwright",
            "test",
            "--config",
            "playwright.installed.config.ts",
        ],
        cwd=REPOSITORY_ROOT / "dashboard-web",
        env=environment,
        timeout=120,
    )


def _proc_start(pid: int) -> str:
    parts = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    if len(parts) < 22:
        raise RuntimeError("owned process start evidence is unavailable")
    return parts[21]


def _write_json_private(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    os.replace(temporary, path)


def _default_registry() -> Path:
    explicit = os.environ.get("PHASE26_DASHBOARD_REGISTRY")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(tempfile.gettempdir()) / f"ai-dev-loop-phase26-dashboard-{os.getuid()}.json"


def _record_prepared(site: InstalledSite, registry: Path) -> dict[str, object]:
    if registry.exists():
        raise RuntimeError(f"prepared-site registry already exists: {registry}")
    token = secrets.token_hex(24)
    payload: dict[str, object] = {
        "schemaVersion": REGISTRY_SCHEMA,
        "status": "manual_pending",
        "resourceRoot": str(site.root.resolve()),
        "ownerUid": os.getuid(),
        "ownerToken": token,
        "pid": site.process.pid,
        "processStart": _proc_start(site.process.pid),
        "port": site.port,
        "executable": str(site.package.cli.resolve()),
        "fixture": str((site.root / "fixture.json").resolve()),
        "createdAt": time.time(),
    }
    owner_path = site.root / ".owner.json"
    _write_json_private(owner_path, payload)
    registry.parent.mkdir(parents=True, exist_ok=True)
    _write_json_private(registry, payload)
    return payload


def _load_cleanup_record(registry: Path) -> tuple[dict[str, object], Path]:
    try:
        payload = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "prepared-site registry is missing or invalid; resources were preserved"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") != REGISTRY_SCHEMA:
        raise RuntimeError("prepared-site registry schema is invalid; resources were preserved")
    if payload.get("ownerUid") != os.getuid():
        raise RuntimeError("prepared-site owner UID does not match; resources were preserved")
    root_value = payload.get("resourceRoot")
    if not isinstance(root_value, str):
        raise RuntimeError("prepared-site resource root is invalid; resources were preserved")
    candidate = Path(root_value)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise RuntimeError("prepared-site resource root is not owned; resources were preserved")
    root = candidate.resolve()
    if root != candidate or not root.name.startswith(RESOURCE_PREFIX) or not root.is_dir():
        raise RuntimeError("prepared-site resource root is not owned; resources were preserved")
    owner_path = root / ".owner.json"
    if not owner_path.is_file() or owner_path.is_symlink():
        raise RuntimeError("prepared-site owner record is invalid; resources were preserved")
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "prepared-site owner record is invalid; resources were preserved"
        ) from exc
    if owner != payload or not secrets.compare_digest(
        str(owner.get("ownerToken", "")), str(payload.get("ownerToken", ""))
    ):
        raise RuntimeError("prepared-site ownership evidence conflicts; resources were preserved")
    return payload, root


def cleanup_prepared(registry: Path) -> dict[str, object]:
    payload, root = _load_cleanup_record(registry)
    pid = payload.get("pid")
    process_start = payload.get("processStart")
    port = payload.get("port")
    executable = payload.get("executable")
    if type(pid) is not int or not isinstance(process_start, str) or type(port) is not int:
        raise RuntimeError("prepared-site process evidence is invalid; resources were preserved")
    proc = Path(f"/proc/{pid}")
    if proc.exists():
        if _proc_start(pid) != process_start:
            raise RuntimeError("prepared-site PID was reused; resources were preserved")
        command = (proc / "cmdline").read_bytes().split(b"\0")
        decoded = [item.decode("utf-8", errors="replace") for item in command if item]
        expected_tail = ["dashboard", "serve", "--port", str(port)]
        script_index = next(
            (
                index
                for index, value in enumerate(decoded[:2])
                if isinstance(executable, str)
                and str(Path(value).resolve()) == str(Path(executable).resolve())
            ),
            None,
        )
        if (
            not isinstance(executable, str)
            or script_index is None
            or decoded[script_index + 1 :] != expected_tail
            or os.getpgid(pid) != pid
        ):
            raise RuntimeError("prepared-site process identity conflicts; resources were preserved")
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while proc.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.exists():
            os.killpg(pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while proc.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        if proc.exists():
            raise RuntimeError(
                "prepared-site process termination is uncertain; resources were preserved"
            )
    shutil.rmtree(root)
    registry.unlink()
    return {"removed": str(root), "recoverable": False, "processWasLive": proc.exists()}


def _list_cases() -> None:
    for name, description in CASES.items():
        print(f"{name}\t{description}")


def _create_resource_root() -> Path:
    path = Path(tempfile.mkdtemp(prefix=RESOURCE_PREFIX, dir="/tmp"))
    if os.name != "nt":
        path.chmod(0o700)
    return path


def _accept(selected: set[str], *, interpreter: Path) -> None:
    root = _create_resource_root()
    site: InstalledSite | None = None
    started = time.monotonic()
    try:
        package = _build_and_install(root, interpreter=interpreter)
        version = ".".join(str(item) for item in package.python_version)
        print(
            f"PASS packaging wheel={package.wheel.name} sdist={package.sdist.name} python={version}"
        )
        if selected != {"packaging"}:
            site = _start_site(root, package)
            if "api" in selected:
                _assert_api(site)
                print(
                    f"PASS api runs={site.fixture.runs_started} reviews={site.fixture.review_rounds} "
                    f"timeline={site.fixture.timeline_total}"
                )
            if "browser" in selected:
                _assert_browser(site, root)
                print("PASS browser installed offline journey=1")
            if "safety" in selected:
                if _authority_snapshot(site.fixture) != site.authority_snapshot:
                    raise RuntimeError("dashboard reads modified authoritative SQLite/artifacts")
                if site.process.poll() is not None:
                    raise RuntimeError("installed dashboard server exited during safety validation")
                print("PASS safety authority=unchanged runtime=loopback/no-node")
    finally:
        if site is not None:
            _stop_process(site.process)
        if root.exists():
            shutil.rmtree(root)
    print(
        f"READY_FOR_REVIEW elapsed={time.monotonic() - started:.2f}s cases={','.join(sorted(selected))}"
    )


def _prepare(registry: Path, *, interpreter: Path) -> None:
    root = _create_resource_root()
    site: InstalledSite | None = None
    try:
        package = _build_and_install(root, interpreter=interpreter)
        site = _start_site(root, package)
        _assert_api(site)
        if _authority_snapshot(site.fixture) != site.authority_snapshot:
            raise RuntimeError("prepared dashboard reads modified authoritative SQLite/artifacts")
        record = _record_prepared(site, registry)
        print(
            "MANUAL_PENDING "
            f"url=http://127.0.0.1:{record['port']}/ resource={record['resourceRoot']} "
            f"registry={registry}"
        )
        site = None  # ownership is now durable and cleanup is a separate command.
    finally:
        if site is not None:
            _stop_process(site.process)
            if root.exists():
                shutil.rmtree(root)
        elif not registry.exists() and root.exists():
            shutil.rmtree(root)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list bounded acceptance cases")
    subparsers = parser.add_subparsers(dest="mode")
    accept = subparsers.add_parser("accept", help="run selected installed acceptance cases")
    accept.add_argument("--case", action="append", choices=tuple(CASES), default=[])
    accept.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="explicit Python >=3.11 interpreter for the isolated wheel runtime",
    )
    prepare = subparsers.add_parser("prepare", help="leave one owned manual-pending site")
    prepare.add_argument("--registry", type=Path, default=None)
    prepare.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="explicit Python >=3.11 interpreter for the isolated wheel runtime",
    )
    cleanup = subparsers.add_parser("cleanup", help="remove only the recorded owned site")
    cleanup.add_argument("--registry", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.list:
        _list_cases()
        return 0
    if args.mode == "accept":
        selected = set(args.case or CASES)
        _accept(selected, interpreter=args.python)
        return 0
    if args.mode == "prepare":
        _prepare((args.registry or _default_registry()).resolve(), interpreter=args.python)
        return 0
    if args.mode == "cleanup":
        receipt = cleanup_prepared((args.registry or _default_registry()).resolve())
        print(f"CLEANED resource={receipt['removed']} recoverable=false")
        return 0
    parser.error("choose accept, prepare, cleanup, or --list")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"BLOCKED_CONTRACT: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

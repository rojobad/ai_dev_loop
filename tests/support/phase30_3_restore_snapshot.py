"""Separate-process public statistics snapshot for a database-only restore.

The parent test starts this file as its own process. It reads scheduler CLI,
Integration inspect, and Dashboard HTTP. It does not migrate, backfill, or
create artifact roots.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from typer.testing import CliRunner

import ai_dev_loop
from ai_dev_loop.cli import app
from ai_dev_loop.dashboard.adapters.http import (
    DashboardHttpDispatcher,
    default_http_service_factories,
)
from ai_dev_loop.integration_api.process_output_auth import authenticate_process_attempt
from ai_dev_loop.scheduler.infrastructure.readonly_protected_artifacts import (
    ReadOnlyProtectedArtifactStore,
)

_RUNNER = CliRunner()
_FORBIDDEN_ENV_PREFIXES = (
    "FAKE_",
    "CURSOR_",
    "OPENAI_",
    "CODEX_",
    "ANTHROPIC_",
    "AZURE_OPENAI_",
)


def recorded_external_roots(database: Path) -> list[Path]:
    """Absolute repository paths frozen in run state payloads."""

    roots: list[Path] = []
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute("SELECT state_payload FROM scheduler_runs")
        for (payload,) in rows:
            data = json.loads(str(payload))
            repository = data.get("context", {}).get("repository", {})
            if not isinstance(repository, dict):
                continue
            for key in ("root", "git_dir", "git_common_dir"):
                value = repository.get(key)
                if isinstance(value, str) and value:
                    roots.append(Path(value))
    return roots


def conceal_external_roots(
    paths: list[Path],
    *,
    checkout: Path,
) -> tuple[list[Path], list[tuple[Path, Path]]]:
    """Rename test-owned roots so restored reads cannot open them.

    The implementation checkout is refused. Nested paths stay in the returned
    list and disappear when their parent is renamed.
    """

    checkout_resolved = checkout.resolve()
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved == checkout_resolved or checkout_resolved in resolved.parents:
            raise AssertionError(f"refusing to hide the implementation checkout: {path}")
        if resolved in checkout_resolved.parents:
            raise AssertionError(f"refusing to hide a parent of the checkout: {path}")
        unique.append(path)
    movable = [
        path
        for path in unique
        if path.exists()
        and not any(path.resolve() != other.resolve() and _within(path, other) for other in unique)
    ]
    moved: list[tuple[Path, Path]] = []
    for path in movable:
        parked = path.with_name(path.name + ".phase303-unavailable")
        if parked.exists():
            raise AssertionError(f"park path already exists: {parked}")
        path.rename(parked)
        moved.append((path, parked))
    for path in unique:
        if path.exists():
            raise AssertionError(f"recorded root still exists: {path}")
    return unique, moved


def reveal_roots(moved: list[tuple[Path, Path]]) -> None:
    for original, parked in reversed(moved):
        if not parked.exists():
            continue
        if original.exists():
            raise AssertionError(f"cannot reveal {original}")
        parked.rename(original)


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return child.resolve() != parent.resolve()


def _forbidden_env() -> list[str]:
    return sorted(key for key in os.environ if key.startswith(_FORBIDDEN_ENV_PREFIXES))


def _fail(message: str) -> None:
    raise SystemExit(message)


def _invoke(args: list[str]) -> object:
    completed = _RUNNER.invoke(app, args)
    if completed.exit_code != 0:
        _fail(f"{args} -> {completed.exit_code}: {completed.stdout}\n{completed.stderr}")
    return json.loads(completed.stdout)


def _database(state_home: Path) -> Path:
    return state_home / "ai_dev_loop" / "engine.sqlite3"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _window(database: Path) -> tuple[str, str]:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        start = conn.execute(
            """
            SELECT MIN(launch_requested_at) FROM scheduler_attempts
            WHERE launch_requested_at IS NOT NULL
            """
        ).fetchone()
    if start is None or not isinstance(start[0], str):
        _fail("snapshot found no execution launch")
    return ("2026-09-01T00:00:00Z", "2027-03-01T00:00:00Z")


def _review_attempt(database: Path, run_id: str) -> str:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        row = conn.execute(
            """
            SELECT attempt.attempt_id
            FROM scheduler_attempts AS attempt
            JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
            WHERE attempt.run_id = ? AND effect.effect_kind = 'codex.bootstrap_review'
            ORDER BY attempt.rowid
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        _fail(f"no review attempt for {run_id}")
    return str(row[0])


def _user_version(database: Path) -> int:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()
    if version is None:
        _fail("missing user_version")
    return int(version[0])


def main() -> None:
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    state_home = Path(str(config["state_home"]))
    database = _database(state_home)
    artifact_root = state_home / "ai_dev_loop" / "artifacts"
    isolate = bool(config["isolate"])
    runs = {str(key): str(value) for key, value in dict(config["runs"]).items()}
    sequence_id = str(config["sequence_id"])
    replacement_sequence_id = str(config["replacement_sequence_id"])
    unavailable = [Path(str(item)) for item in list(config["unavailable_paths"])]
    forbidden_env = _forbidden_env()
    if isolate and (not unavailable or forbidden_env or any(path.exists() for path in unavailable)):
        present = [str(path) for path in unavailable if path.exists()]
        _fail(f"restored read is not isolated: env={forbidden_env} paths={present}")
    if not Path(ai_dev_loop.__file__).is_file():
        _fail("package under test is not readable")
    before = _sha256(database)
    version_before = _user_version(database)
    start, end = _window(database)
    opener_calls = 0

    def refuse(*_args: object, **_kwargs: object) -> None:
        nonlocal opener_calls
        opener_calls += 1
        raise AssertionError("sqlite statistics opened a usage source")

    original_auth = authenticate_process_attempt
    original_read = ReadOnlyProtectedArtifactStore.read_verified_bytes
    if isolate:
        import ai_dev_loop.scheduler.application.token_usage_query as query_module

        query_module.authenticate_process_attempt = refuse  # type: ignore[assignment]
        ReadOnlyProtectedArtifactStore.read_verified_bytes = refuse  # type: ignore[method-assign]

    try:
        usage = {
            name: _invoke(["scheduler", "usage", run_id, "--output", "json"])
            for name, run_id in sorted(runs.items())
        }
        inspected = {
            name: _invoke(["integration", "run", "inspect", run_id])
            for name, run_id in sorted(runs.items())
        }
        listed = _invoke(["scheduler", "list", "--output", "json"])
        statuses = {
            name: _invoke(["scheduler", "status", run_id, "--output", "json"])
            for name, run_id in sorted(runs.items())
        }
        timelines = {
            name: _invoke(["scheduler", "timeline", run_id, "--output", "json"])
            for name, run_id in sorted(runs.items())
        }
        sequence_usage = _invoke(
            ["scheduler", "sequence", "usage", sequence_id, "--output", "json"]
        )
        sequence_inspect = _invoke(["integration", "sequence", "inspect", sequence_id])
        sequence_status = _invoke(
            ["scheduler", "sequence", "status", sequence_id, "--output", "json"]
        )
        replacement_sequence_usage = _invoke(
            ["scheduler", "sequence", "usage", replacement_sequence_id, "--output", "json"]
        )
        replacement_sequence_inspect = _invoke(
            ["integration", "sequence", "inspect", replacement_sequence_id]
        )
        replacement_sequence_status = _invoke(
            ["scheduler", "sequence", "status", replacement_sequence_id, "--output", "json"]
        )
        http = DashboardHttpDispatcher(
            default_http_service_factories(db_path=database, artifact_root=artifact_root)
        )
        filters = {"fromUtc": start, "toUtc": end, "timeZone": "UTC"}
        santiago = {**filters, "timeZone": "America/Santiago"}
        dashboard = {
            "projects": http.dispatch("dashboard.projects", {"offset": 0, "limit": 50}),
            "summary": http.dispatch("dashboard.summary", filters),
            "summary_santiago": http.dispatch("dashboard.summary", santiago),
            "runs": http.dispatch("dashboard.runs", {**filters, "offset": 0, "limit": 100}),
            "tokens": http.dispatch("dashboard.token-usage", filters),
            "tokens_santiago": http.dispatch("dashboard.token-usage", santiago),
            "tokens_outside": http.dispatch(
                "dashboard.token-usage",
                {
                    "fromUtc": "2020-01-01T00:00:00Z",
                    "toUtc": "2020-01-02T00:00:00Z",
                    "timeZone": "UTC",
                },
            ),
            "tokens_by_run": {
                name: http.dispatch(
                    "dashboard.token-usage",
                    {**filters, "runId": run_id},
                )
                for name, run_id in sorted(runs.items())
            },
            "tokens_by_project": {
                project: http.dispatch(
                    "dashboard.token-usage",
                    {**filters, "projectName": project},
                )
                for project in ("fixture-alpha", "fixture-beta")
            },
            "activity": {
                zone: {
                    metric: http.dispatch(
                        "dashboard.activity",
                        {
                            "fromUtc": start,
                            "toUtc": end,
                            "timeZone": zone,
                            "metric": metric,
                        },
                    )
                    for metric in ("submissions", "review-rounds", "process-hours")
                }
                for zone in ("UTC", "America/Santiago")
            },
        }
    finally:
        if isolate:
            import ai_dev_loop.scheduler.application.token_usage_query as query_module

            query_module.authenticate_process_attempt = original_auth  # type: ignore[assignment]
            ReadOnlyProtectedArtifactStore.read_verified_bytes = original_read  # type: ignore[method-assign]

    review_run_id = str(config["review_run_id"])
    review_attempt = _review_attempt(database, review_run_id)
    review_content = _invoke(
        [
            "integration",
            "run",
            "review-content",
            review_run_id,
            "--attempt",
            review_attempt,
            "--kind",
            "prompt",
        ]
    )
    initial_prompt = None
    if isolate:
        completed = _RUNNER.invoke(
            app,
            ["integration", "run", "initial-prompt", review_run_id, "--output", "json"],
        )
        body = json.loads(completed.stdout) if completed.stdout.strip() else {}
        error = body.get("error") if isinstance(body, dict) else None
        initial_prompt = {
            "exit_code": completed.exit_code,
            "ok": body.get("ok") if isinstance(body, dict) else None,
            "code": error.get("code") if isinstance(error, dict) else None,
            "data": body.get("data") if isinstance(body, dict) else None,
        }
    readiness = _invoke(["scheduler", "usage-backfill", "--all", "--check", "--output", "json"])
    after = _sha256(database)
    payload = {
        "schema_version": version_before,
        "schema_version_after": _user_version(database),
        "database_sha256_before": before,
        "database_sha256_after": after,
        "artifact_root_exists": artifact_root.exists(),
        "opener_calls": opener_calls,
        "usage": usage,
        "inspected": inspected,
        "listed": listed,
        "statuses": statuses,
        "timelines": timelines,
        "sequence_usage": sequence_usage,
        "sequence_inspect": sequence_inspect,
        "sequence_status": sequence_status,
        "replacement_sequence_usage": replacement_sequence_usage,
        "replacement_sequence_inspect": replacement_sequence_inspect,
        "replacement_sequence_status": replacement_sequence_status,
        "forbidden_env": forbidden_env,
        "unavailable_missing": [str(path) for path in unavailable if not path.exists()],
        "unavailable_present": [str(path) for path in unavailable if path.exists()],
        "package_origin": str(Path(ai_dev_loop.__file__).resolve()),
        "dashboard": dashboard,
        "review_content": review_content,
        "initial_prompt": initial_prompt,
        "readiness": readiness,
    }
    sys.stdout.write(json.dumps(payload))


if __name__ == "__main__":
    main()

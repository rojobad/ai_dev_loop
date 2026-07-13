"""Run discovery helpers."""

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.paths import runs_dir
from ai_dev_loop.resume_planner import TERMINAL_STATUSES
from ai_dev_loop.state import RunState, load_run_state


def find_run_directory(run_id: str) -> Path:
    matches: list[Path] = []
    root = runs_dir()
    if not root.is_dir():
        raise ValidationError(f"run not found: {run_id}")
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / run_id
        if candidate.is_dir() and (candidate / "state.json").is_file():
            matches.append(candidate)
    if not matches:
        raise ValidationError(f"run not found: {run_id}")
    if len(matches) > 1:
        raise ValidationError(f"run id is ambiguous across projects: {run_id}")
    return matches[0]


def load_run(run_id: str) -> tuple[Path, RunState]:
    run_path = find_run_directory(run_id)
    state = load_run_state(run_path / "state.json")
    return run_path, state


def list_run_directories(
    *,
    project: str | None = None,
    status: str | None = None,
) -> list[tuple[Path, RunState]]:
    results: list[tuple[Path, RunState]] = []
    root = runs_dir()
    if not root.is_dir():
        return results
    project_dirs = [root / project] if project else sorted(root.iterdir())
    for project_dir in project_dirs:
        if not project_dir.is_dir():
            continue
        for run_path in sorted(project_dir.iterdir(), reverse=True):
            state_file = run_path / "state.json"
            if not state_file.is_file():
                continue
            state = load_run_state(state_file)
            if status is not None and state.status.value != status:
                continue
            results.append((run_path, state))
    return results


def find_runs_for_controller(
    *,
    controller_session_id: str,
    repository_root: Path,
    include_terminal: bool = False,
) -> list[tuple[Path, RunState]]:
    """Return runs whose persisted controller ID and repository root match exactly.

    Never selects by timestamp. Callers must handle zero or multiple matches.
    """

    repo_root = repository_root.resolve()
    matches: list[tuple[Path, RunState]] = []
    for run_path, state in list_run_directories():
        if state.controller is None:
            continue
        if state.controller.controller_session_id != controller_session_id:
            continue
        if Path(state.repository.root).resolve() != repo_root:
            continue
        if not include_terminal and state.status in TERMINAL_STATUSES:
            continue
        matches.append((run_path, state))
    return matches

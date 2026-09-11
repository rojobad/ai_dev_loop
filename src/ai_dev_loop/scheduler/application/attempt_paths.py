"""Owner-only attempt artifact path preparation."""

from __future__ import annotations

import os
from pathlib import Path

from ai_dev_loop.paths import DIR_MODE, SENSITIVE_FILE_MODE
from ai_dev_loop.scheduler.application.attempt_envelope import (
    attempt_result_rel,
    attempt_stderr_rel,
    attempt_stdout_rel,
)
from ai_dev_loop.scheduler.infrastructure.paths import resolve_run_relative_path


def prepare_attempt_output_paths(
    run_root: Path,
    attempt_id: str,
) -> tuple[Path, Path, Path, str, str, str]:
    """Create attempt output directories and empty streams with owner-only permissions."""

    stdout_rel = attempt_stdout_rel(attempt_id)
    stderr_rel = attempt_stderr_rel(attempt_id)
    result_rel = attempt_result_rel(attempt_id)
    stdout_path = resolve_run_relative_path(run_root, stdout_rel)
    stderr_path = resolve_run_relative_path(run_root, stderr_rel)
    result_path = resolve_run_relative_path(run_root, result_rel)
    for directory in (stdout_path.parent, stderr_path.parent, result_path.parent):
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, DIR_MODE)
    for path in (stdout_path, stderr_path):
        if not path.exists():
            path.write_text("", encoding="utf-8")
        if os.name != "nt":
            os.chmod(path, SENSITIVE_FILE_MODE)
    if result_path.exists():
        raise ValueError("result envelope path already exists")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(result_path.parent, DIR_MODE)
    return stdout_path, stderr_path, result_path, stdout_rel, stderr_rel, result_rel

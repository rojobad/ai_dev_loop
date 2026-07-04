"""Direct subprocess execution helpers."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from ai_dev_loop.errors import AiDevLoopError


@dataclass(frozen=True)
class ProcessResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_process(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    try:
        completed = subprocess.run(
            list(args),
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode()
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode()
        return ProcessResult(
            args=list(args),
            returncode=124,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
        )
    except FileNotFoundError as exc:
        raise AiDevLoopError(f"executable not found: {args[0]}") from exc

    return ProcessResult(
        args=list(args),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def require_success(result: ProcessResult, *, context: str) -> str:
    if result.timed_out:
        raise AiDevLoopError(f"{context}: command timed out: {' '.join(result.args)}")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise AiDevLoopError(f"{context}: {' '.join(result.args)} failed: {detail}")
    return result.stdout.strip()

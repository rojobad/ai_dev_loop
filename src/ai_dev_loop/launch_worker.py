"""Detached worker entry point for `ai_dev_loop launch`.

Invoked as::

    python -m ai_dev_loop.launch_worker <run-id> <worker-token> [policy-path]

The worker waits until the parent publishes a matching ``launcher.json`` before
calling ``start_run``. Completion updates the launcher record only when the
worker token and PID still match.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.launcher import (
    mark_launcher_finished,
    read_launcher_policy,
    wait_for_launcher_ready,
)
from ai_dev_loop.run_discovery import find_run_directory, load_run
from ai_dev_loop.runners.tool_updates import ToolCompatibilityPolicy, UpdateMode
from ai_dev_loop.workflow_engine import start_run


def _policy_from_path(path: Path | None) -> ToolCompatibilityPolicy:
    if path is None:
        return ToolCompatibilityPolicy(update_mode=UpdateMode.NEVER, allow_incompatible=False)
    serialized = read_launcher_policy(path)
    mode = UpdateMode.ALWAYS if serialized.update_mode == "always" else UpdateMode.NEVER
    return ToolCompatibilityPolicy(
        update_mode=mode,
        allow_incompatible=serialized.allow_incompatible,
        ask_callback=None,
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or not args[0].strip() or not args[1].strip():
        print(
            "usage: python -m ai_dev_loop.launch_worker <run-id> <worker-token> [policy-path]",
            file=sys.stderr,
        )
        return 2
    run_id = args[0].strip()
    worker_token = args[1].strip()
    policy_path = Path(args[2]) if len(args) >= 3 and args[2].strip() else None

    run_directory = find_run_directory(run_id)
    workflow_status: str | None = None
    safe_error: str | None = None
    exit_code = 1
    ready = False
    try:
        wait_for_launcher_ready(
            run_directory,
            run_id=run_id,
            worker_token=worker_token,
        )
        ready = True
        policy = _policy_from_path(policy_path)
        result = start_run(run_id, tool_policy=policy)
        workflow_status = result.status
        _, state = load_run(run_id)
        safe_error = state.last_error
        exit_code = 1 if result.status in {"failed", "aborted"} else 0
    except AiDevLoopError as exc:
        safe_error = str(exc)
        exit_code = 1
        try:
            _, state = load_run(run_id)
            workflow_status = state.status.value
            if state.last_error:
                safe_error = state.last_error
        except Exception:
            pass
    except Exception:
        safe_error = "detached worker failed; inspect launcher and run artifacts"
        exit_code = 1
        try:
            _, state = load_run(run_id)
            workflow_status = state.status.value
        except Exception:
            pass
    finally:
        if ready:
            mark_launcher_finished(
                run_directory,
                run_id=run_id,
                worker_token=worker_token,
                pid=os.getpid(),
                exit_code=exit_code,
                workflow_status=workflow_status,
                safe_error=safe_error,
            )
    return exit_code


def _entrypoint() -> NoReturn:
    raise SystemExit(main())


if __name__ == "__main__":
    _entrypoint()

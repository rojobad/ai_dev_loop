#!/usr/bin/env python3
"""Controlled supervisor worker for the Phase 16.8 existing-PR happy-path Gate A test.

Mirrors ``ai_dev_loop.pr_review_v2_supervisor_worker`` ownership/token checks, but
reassembles the same temporary existing-PR stack (LOCAL git, stateful fake gh,
ProcessCodexRunner + fake Codex) instead of production SSH assembly.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

# Ensure repository root is importable when launched as a script path.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.integration.phase16_8_existing_pr_happy_path_helpers import (  # noqa: E402
    assemble_existing_pr_happy_path_stack_from_config,
)

from ai_dev_loop.pr_review_v2.workers.supervisor import (  # noqa: E402
    PrReviewV2Supervisor,
    SupervisorLauncherStore,
    validate_launcher_ownership,
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or not args[0].strip() or not args[1].strip():
        print(
            "usage: phase16_8_existing_pr_happy_path_worker.py <run-id> <token>",
            file=sys.stderr,
        )
        return 2
    run_id = args[0].strip()
    token = args[1].strip()
    config_path = Path(os.environ["HAPPY_PATH_STACK_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    artifact_root = Path(config["artifact_root"])
    launchers = SupervisorLauncherStore(artifact_root)
    meta = None
    for _ in range(50):
        meta = launchers.read(run_id)
        if meta is not None and validate_launcher_ownership(
            meta, run_id=run_id, expected_token=token
        ):
            break
        time.sleep(0.05)
    else:
        print("supervisor launcher metadata missing or token mismatch", file=sys.stderr)
        return 1

    cancel = {"requested": False}

    def _on_signal(signum: int, _frame: object) -> None:
        del signum
        cancel["requested"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    try:
        stack = assemble_existing_pr_happy_path_stack_from_config(config, run_id=run_id)

        def _sleep(seconds: float) -> None:
            stack.clock.advance(max(seconds, 0.0))

        kind = PrReviewV2Supervisor(
            stack.engine,
            stack.worker,
            run_id=run_id,
            idle_poll_seconds=1,
            clock=stack.clock,
            max_steps=500,
            sleep=_sleep,
            cancel_check=lambda: cancel["requested"],
        ).run_until_idle()
        if kind != "completed":
            print(f"supervisor exited non-completed: {kind}", file=sys.stderr)
            return 1
    except Exception as exc:  # noqa: BLE001
        print(f"pr-review-v2 happy-path supervisor failed: {exc}", file=sys.stderr)
        return 1
    finally:
        current = launchers.read(run_id)
        if current is not None and current.token == token and current.pid == meta.pid:
            launchers.clear(run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Detached worker entry point for PR review v2 supervisor loops."""

from __future__ import annotations

import signal
import sys
import time

from ai_dev_loop.pr_review_v2.infrastructure.paths import pr_review_v2_state_dir
from ai_dev_loop.pr_review_v2.runtime_factory import assemble_supervisor_runtime
from ai_dev_loop.pr_review_v2.workers.supervisor import (
    PrReviewV2Supervisor,
    SupervisorLauncherStore,
    validate_launcher_ownership,
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or not args[0].strip() or not args[1].strip():
        print(
            "usage: python -m ai_dev_loop.pr_review_v2_supervisor_worker <run-id> <token>",
            file=sys.stderr,
        )
        return 2
    run_id = args[0].strip()
    token = args[1].strip()
    artifact_root = pr_review_v2_state_dir() / "artifacts"
    launchers = SupervisorLauncherStore(artifact_root)
    # Parent persists launcher metadata after Popen; wait briefly for the write.
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
        runtime = assemble_supervisor_runtime(run_id)
        PrReviewV2Supervisor(
            runtime.engine,
            runtime.worker,
            run_id=run_id,
            idle_poll_seconds=runtime.idle_poll_seconds,
            cancel_check=lambda: cancel["requested"],
        ).run_until_idle()
    except Exception as exc:  # noqa: BLE001
        print(f"pr-review supervisor failed: {exc}", file=sys.stderr)
        return 1
    finally:
        current = launchers.read(run_id)
        if current is not None and current.token == token and current.pid == meta.pid:
            launchers.clear(run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

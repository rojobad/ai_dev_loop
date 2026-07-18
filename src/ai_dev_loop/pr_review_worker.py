"""Detached worker entry point for GitHub PR-review polling and publication."""

from __future__ import annotations

import sys

from ai_dev_loop.commands.pr_review import run_pr_review_worker_loop
from ai_dev_loop.errors import AiDevLoopError


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or not args[0].strip() or not args[1].strip():
        print(
            "usage: python -m ai_dev_loop.pr_review_worker <run-id> <worker-token>",
            file=sys.stderr,
        )
        return 2
    run_id = args[0].strip()
    try:
        run_pr_review_worker_loop(run_id)
    except AiDevLoopError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"PR-review worker failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

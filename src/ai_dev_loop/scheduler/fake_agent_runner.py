"""Minimal fake agent runner for scheduler attempt executor acceptance."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.attempt_envelope import (
    build_result_envelope,
    envelope_sha256,
    sha256_file,
)
from ai_dev_loop.scheduler.application.attempt_identity import (
    validate_attempt_id,
    validate_unit_identity,
)
from ai_dev_loop.scheduler.application.attempt_paths import prepare_attempt_output_paths
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.state import atomic_write_bytes


def _termination_for_exit(exit_code: int) -> TerminationClass:
    if exit_code == 0:
        return TerminationClass.SUCCESS
    if exit_code < 0:
        return TerminationClass.KILLED
    return TerminationClass.NONZERO_EXIT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scheduler fake agent runner")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--unit-identity", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--exit-code", type=int, default=None)
    args = parser.parse_args(argv)

    attempt_id = validate_attempt_id(args.attempt_id)
    unit_identity = validate_unit_identity(args.unit_identity)
    artifact_root = Path(args.artifact_root)
    run_root = run_artifact_root(artifact_root, args.run_id)

    stdout_path, stderr_path, result_path, stdout_rel, stderr_rel, _result_rel = (
        prepare_attempt_output_paths(run_root, attempt_id)
    )
    stdout_path.write_text("fake-agent-stdout\n", encoding="utf-8")
    stderr_path.write_text("fake-agent-stderr\n", encoding="utf-8")

    exit_code = args.exit_code
    if exit_code is None:
        mode = os.environ.get("SCHEDULER_FAKE_AGENT_MODE", "success")
        if mode == "nonzero":
            exit_code = 2
        elif mode == "timeout":
            exit_code = 124
        else:
            exit_code = 0

    termination = _termination_for_exit(exit_code)
    if os.environ.get("SCHEDULER_FAKE_AGENT_MODE") == "timeout":
        termination = TerminationClass.TIMEOUT

    stdout_sha = sha256_file(stdout_path)
    stderr_sha = sha256_file(stderr_path)
    envelope = build_result_envelope(
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        exit_code=exit_code,
        termination_class=termination,
        stdout_artifact_path=stdout_rel,
        stdout_sha256=stdout_sha,
        stderr_artifact_path=stderr_rel,
        stderr_sha256=stderr_sha,
    )
    atomic_write_bytes(result_path, envelope)
    _ = envelope_sha256(envelope)
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())

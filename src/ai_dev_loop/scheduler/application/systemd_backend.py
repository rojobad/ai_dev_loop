"""Production systemd-user transient unit backend for scheduler attempts."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ai_dev_loop.process import ProcessResult, run_process
from ai_dev_loop.scheduler.application.attempt_backend import (
    AgentProcessBackend,
    LaunchRequest,
    ObserveResult,
    UnitLifecycleState,
)
from ai_dev_loop.scheduler.application.attempt_identity import (
    flock_wrapped_argv,
    unit_identity_from_attempt_id,
    validate_attempt_id,
    validate_unit_identity,
)
from ai_dev_loop.scheduler.application.systemd_show import observe_from_show, parse_systemctl_show

ATTEMPT_FINALIZATION_GRACE_SECONDS = 300
ATTEMPT_STOP_GRACE_SECONDS = 120
_SHOW_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainStatus",
    "ExecMainCode",
)


@dataclass
class SystemdUserBackend(AgentProcessBackend):
    systemd_run: Sequence[str] = field(default_factory=lambda: ("systemd-run",))
    systemctl: Sequence[str] = field(default_factory=lambda: ("systemctl", "--user"))
    runner: Callable[..., ProcessResult] = run_process
    fake_agent_module: str = "ai_dev_loop.scheduler.fake_agent_runner"
    finalization_grace_seconds: int = ATTEMPT_FINALIZATION_GRACE_SECONDS
    stop_grace_seconds: int = ATTEMPT_STOP_GRACE_SECONDS
    launch_timeout_seconds: float = 30.0
    observe_timeout_seconds: float = 10.0
    terminate_timeout_seconds: float = 30.0

    def build_agent_argv(
        self, *, run_id: str, attempt_id: str, unit_identity: str, artifact_root: Path
    ) -> list[str]:
        return [
            sys.executable,
            "-m",
            self.fake_agent_module,
            "--run-id",
            run_id,
            "--attempt-id",
            attempt_id,
            "--unit-identity",
            unit_identity,
            "--artifact-root",
            str(artifact_root),
        ]

    @staticmethod
    def runtime_envelope_seconds(execution_timeout_seconds: int, *, grace_seconds: int) -> int:
        if execution_timeout_seconds <= 0:
            raise ValueError("execution_timeout_seconds must be a positive integer")
        if grace_seconds <= 0:
            raise ValueError("grace_seconds must be a positive integer")
        return execution_timeout_seconds + grace_seconds

    @staticmethod
    def validated_stop_grace_seconds(stop_grace_seconds: int) -> int:
        if stop_grace_seconds <= 0:
            raise ValueError("stop_grace_seconds must be a positive integer")
        return stop_grace_seconds

    def launch(self, request: LaunchRequest) -> None:
        validate_attempt_id(request.attempt_id)
        validate_unit_identity(request.unit_identity)
        if request.unit_identity != unit_identity_from_attempt_id(request.attempt_id):
            raise ValueError("unit_identity does not match attempt_id")
        runtime_envelope = self.runtime_envelope_seconds(
            request.execution_timeout_seconds,
            grace_seconds=self.finalization_grace_seconds,
        )
        stop_grace = self.validated_stop_grace_seconds(self.stop_grace_seconds)
        request.working_directory.mkdir(parents=True, exist_ok=True)
        request.lock_path.parent.mkdir(parents=True, exist_ok=True)
        wrapped = flock_wrapped_argv(request.lock_path, request.agent_argv)
        args = [
            *self.systemd_run,
            "--user",
            f"--unit={request.unit_identity}",
            f"--working-directory={request.working_directory}",
            f"--property=StandardOutput=file:{request.stdout_path}",
            f"--property=StandardError=file:{request.stderr_path}",
            "--property=RemainAfterExit=yes",
            f"--property=TimeoutStartSec={runtime_envelope}",
            f"--property=RuntimeMaxSec={runtime_envelope}",
            f"--property=TimeoutStopSec={stop_grace}",
            "--",
            *wrapped,
        ]
        result = self.runner(args, timeout=self.launch_timeout_seconds)
        if result.timed_out or result.returncode != 0:
            raise RuntimeError("systemd-run launch failed")

    def observe(self, *, unit_identity: str, attempt_id: str) -> ObserveResult:
        validate_attempt_id(attempt_id)
        validate_unit_identity(unit_identity)
        if unit_identity != unit_identity_from_attempt_id(attempt_id):
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.MISSING,
                owned=False,
                absence_proven=False,
            )
        args = [*self.systemctl, "show", unit_identity]
        for property_name in _SHOW_PROPERTIES:
            args.extend(["-p", property_name])
        result = self.runner(args, timeout=self.observe_timeout_seconds)
        if result.timed_out or result.returncode != 0:
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.UNAVAILABLE,
                owned=False,
                absence_proven=False,
            )
        show = parse_systemctl_show(result.stdout)
        parsed = observe_from_show(show=show, expected_unit_identity=unit_identity)
        return ObserveResult(
            lifecycle_state=parsed.lifecycle_state,
            owned=parsed.owned,
            absence_proven=parsed.absence_proven,
            exit_code=parsed.exit_code,
            termination_class=parsed.termination_class,
        )

    def terminate(self, *, unit_identity: str, attempt_id: str) -> None:
        validate_attempt_id(attempt_id)
        validate_unit_identity(unit_identity)
        if unit_identity != unit_identity_from_attempt_id(attempt_id):
            raise ValueError("unit_identity does not match attempt_id")
        observation = self.observe(unit_identity=unit_identity, attempt_id=attempt_id)
        if observation.lifecycle_state == UnitLifecycleState.UNAVAILABLE or not observation.owned:
            raise RuntimeError("refusing to terminate unit without authoritative observation")
        if observation.lifecycle_state == UnitLifecycleState.MISSING and observation.absence_proven:
            return
        if observation.lifecycle_state == UnitLifecycleState.MISSING:
            raise RuntimeError("refusing to terminate unit without authoritative observation")
        args = [*self.systemctl, "stop", unit_identity]
        result = self.runner(args, timeout=self.terminate_timeout_seconds)
        if result.timed_out:
            raise RuntimeError("systemctl stop timed out")
        if result.returncode != 0:
            raise RuntimeError("systemctl stop failed")

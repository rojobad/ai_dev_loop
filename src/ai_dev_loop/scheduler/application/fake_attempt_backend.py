"""Deterministic fake agent process backend for scheduler tests."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ai_dev_loop.paths import SENSITIVE_FILE_MODE
from ai_dev_loop.scheduler.application.attempt_backend import (
    AgentProcessBackend,
    LaunchRequest,
    ObserveResult,
    TerminationClass,
    UnitLifecycleState,
)
from ai_dev_loop.scheduler.application.attempt_envelope import (
    AttemptResultEnvelope,
    attempt_stderr_rel,
    attempt_stdout_rel,
    build_result_envelope,
    envelope_sha256,
    parse_result_envelope,
    sha256_file,
)
from ai_dev_loop.scheduler.application.attempt_identity import (
    unit_identity_from_attempt_id,
    validate_attempt_id,
    validate_unit_identity,
)


@dataclass
class FakeAttemptScenario:
    mode: str = "success"
    exit_code: int = 0
    termination_class: TerminationClass = TerminationClass.SUCCESS
    active_ticks: int = 1
    owned: bool = True
    missing_after_launch: bool = False
    stale_identity: bool = False
    observation_unavailable: bool = False
    launch_raises: bool = False


@dataclass
class FakeAgentProcessBackend(AgentProcessBackend):
    """In-memory backend with controllable lifecycle scenarios."""

    scenarios: dict[str, FakeAttemptScenario] = field(default_factory=dict)
    default_scenario: FakeAttemptScenario = field(default_factory=FakeAttemptScenario)
    launch_calls: list[LaunchRequest] = field(default_factory=list)
    observe_calls: list[tuple[str, str]] = field(default_factory=list)
    terminate_calls: list[tuple[str, str]] = field(default_factory=list)
    _launched: dict[str, LaunchRequest] = field(default_factory=dict)
    _observe_counts: dict[str, int] = field(default_factory=dict)
    _completed: dict[str, ObserveResult] = field(default_factory=dict)

    def set_scenario(self, attempt_id: str, scenario: FakeAttemptScenario) -> None:
        validate_attempt_id(attempt_id)
        self.scenarios[attempt_id] = scenario

    def launch(self, request: LaunchRequest) -> None:
        validate_attempt_id(request.attempt_id)
        validate_unit_identity(request.unit_identity)
        expected = unit_identity_from_attempt_id(request.attempt_id)
        if request.unit_identity != expected:
            raise ValueError("unit_identity does not match attempt_id")
        scenario = self.scenarios.get(request.attempt_id, self.default_scenario)
        if scenario.launch_raises:
            raise RuntimeError("fake launch failure")
        self.launch_calls.append(request)
        self._launched[request.attempt_id] = request
        self._observe_counts.setdefault(request.attempt_id, 0)

    def observe(self, *, unit_identity: str, attempt_id: str) -> ObserveResult:
        validate_attempt_id(attempt_id)
        validate_unit_identity(unit_identity)
        self.observe_calls.append((unit_identity, attempt_id))
        scenario = self.scenarios.get(attempt_id, self.default_scenario)
        if scenario.observation_unavailable:
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.UNAVAILABLE,
                owned=False,
                absence_proven=False,
            )
        if scenario.stale_identity:
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.MISSING,
                owned=False,
                absence_proven=False,
            )
        if not scenario.owned:
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.ACTIVE,
                owned=False,
                absence_proven=False,
            )
        if attempt_id not in self._launched:
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.MISSING,
                owned=True,
                absence_proven=True,
            )
        request = self._launched[attempt_id]
        if attempt_id in self._completed:
            return self._completed[attempt_id]
        if scenario.missing_after_launch and attempt_id not in self._completed:
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.MISSING,
                owned=True,
                absence_proven=True,
            )
        count = self._observe_counts.get(attempt_id, 0) + 1
        self._observe_counts[attempt_id] = count
        if count <= scenario.active_ticks:
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.ACTIVE,
                owned=True,
                absence_proven=False,
            )
        if attempt_id in self._completed:
            return self._completed[attempt_id]
        envelope_path = request.result_envelope_path
        stdout_path = request.stdout_path
        stderr_path = request.stderr_path
        stdout_rel = attempt_stdout_rel(attempt_id)
        stderr_rel = attempt_stderr_rel(attempt_id)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text("fake-agent-stdout\n", encoding="utf-8")
        stderr_path.write_text("fake-agent-stderr\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(stdout_path, SENSITIVE_FILE_MODE)
            os.chmod(stderr_path, SENSITIVE_FILE_MODE)
        stdout_sha = sha256_file(stdout_path)
        stderr_sha = sha256_file(stderr_path)
        envelope = build_result_envelope(
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            exit_code=scenario.exit_code,
            termination_class=scenario.termination_class,
            stdout_artifact_path=stdout_rel,
            stdout_sha256=stdout_sha,
            stderr_artifact_path=stderr_rel,
            stderr_sha256=stderr_sha,
        )
        envelope_path.parent.mkdir(parents=True, exist_ok=True)
        envelope_path.write_bytes(envelope)
        if os.name != "nt":
            os.chmod(envelope_path, SENSITIVE_FILE_MODE)
        _ = envelope_sha256(envelope)
        lifecycle = (
            UnitLifecycleState.INACTIVE
            if scenario.termination_class == TerminationClass.SUCCESS
            else UnitLifecycleState.FAILED
        )
        result = ObserveResult(
            lifecycle_state=lifecycle,
            owned=True,
            absence_proven=False,
            exit_code=scenario.exit_code,
            termination_class=scenario.termination_class,
            result_envelope_path=envelope_path,
        )
        self._completed[attempt_id] = result
        return result

    def terminate(self, *, unit_identity: str, attempt_id: str) -> None:
        validate_attempt_id(attempt_id)
        validate_unit_identity(unit_identity)
        expected = unit_identity_from_attempt_id(attempt_id)
        if unit_identity != expected:
            raise ValueError("unit_identity does not match attempt_id")
        scenario = self.scenarios.get(attempt_id, self.default_scenario)
        if scenario.observation_unavailable:
            raise RuntimeError("refusing to terminate unit without authoritative observation")
        if scenario.stale_identity or not scenario.owned:
            raise RuntimeError("refusing to terminate unit without validated ownership")
        if attempt_id not in self._launched:
            return
        self.terminate_calls.append((unit_identity, attempt_id))
        self._completed[attempt_id] = ObserveResult(
            lifecycle_state=UnitLifecycleState.INACTIVE,
            owned=True,
            absence_proven=False,
            exit_code=scenario.exit_code,
            termination_class=TerminationClass.KILLED,
            result_envelope_path=self._launched[attempt_id].result_envelope_path,
        )

    def preload_completed(
        self,
        attempt_id: str,
        *,
        envelope_path: Path,
        exit_code: int = 0,
        termination_class: TerminationClass = TerminationClass.SUCCESS,
    ) -> None:
        validate_attempt_id(attempt_id)
        self._completed[attempt_id] = ObserveResult(
            lifecycle_state=UnitLifecycleState.INACTIVE,
            owned=True,
            absence_proven=False,
            exit_code=exit_code,
            termination_class=termination_class,
            result_envelope_path=envelope_path,
        )

    def read_envelope(self, path: Path) -> AttemptResultEnvelope:
        return parse_result_envelope(path.read_bytes())

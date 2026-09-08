"""Parse systemctl show output into named properties."""

from __future__ import annotations

from dataclasses import dataclass

from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass, UnitLifecycleState

_RUNNING_SUB_STATES = frozenset(
    {"running", "start", "start-pre", "start-post", "auto-restart", "reload", "reload-notify"}
)


@dataclass(frozen=True)
class SystemdUnitShow:
    unit_id: str | None
    active_state: str | None
    sub_state: str | None
    result: str | None
    exec_main_status: int | None
    exec_main_code: int | None
    load_state: str | None


@dataclass(frozen=True)
class ParsedUnitObservation:
    lifecycle_state: UnitLifecycleState
    owned: bool
    absence_proven: bool
    exit_code: int | None
    termination_class: TerminationClass | None


def parse_systemctl_show(stdout: str) -> SystemdUnitShow:
    properties: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        properties[key.strip()] = value.strip()
    exec_main_status = _parse_int(properties.get("ExecMainStatus"))
    exec_main_code = _parse_int(properties.get("ExecMainCode"))
    return SystemdUnitShow(
        unit_id=properties.get("Id"),
        active_state=properties.get("ActiveState"),
        sub_state=properties.get("SubState"),
        result=properties.get("Result"),
        exec_main_status=exec_main_status,
        exec_main_code=exec_main_code,
        load_state=properties.get("LoadState"),
    )


def _parse_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def observe_from_show(
    *,
    show: SystemdUnitShow,
    expected_unit_identity: str,
) -> ParsedUnitObservation:
    if show.load_state == "not-found":
        return ParsedUnitObservation(
            lifecycle_state=UnitLifecycleState.MISSING,
            owned=True,
            absence_proven=True,
            exit_code=None,
            termination_class=None,
        )
    if show.unit_id is not None and show.unit_id != expected_unit_identity:
        return ParsedUnitObservation(
            lifecycle_state=UnitLifecycleState.MISSING,
            owned=False,
            absence_proven=False,
            exit_code=None,
            termination_class=None,
        )
    if show.active_state is None and show.load_state is None:
        return ParsedUnitObservation(
            lifecycle_state=UnitLifecycleState.UNAVAILABLE,
            owned=False,
            absence_proven=False,
            exit_code=None,
            termination_class=None,
        )
    if show.active_state in {"activating", "deactivating", "reloading"}:
        return ParsedUnitObservation(
            lifecycle_state=UnitLifecycleState.ACTIVE,
            owned=True,
            absence_proven=False,
            exit_code=None,
            termination_class=None,
        )
    if show.active_state == "active":
        if show.sub_state in _RUNNING_SUB_STATES:
            return ParsedUnitObservation(
                lifecycle_state=UnitLifecycleState.ACTIVE,
                owned=True,
                absence_proven=False,
                exit_code=None,
                termination_class=None,
            )
        if show.sub_state == "exited":
            return _completed_observation(show)
        return ParsedUnitObservation(
            lifecycle_state=UnitLifecycleState.UNAVAILABLE,
            owned=False,
            absence_proven=False,
            exit_code=None,
            termination_class=None,
        )
    if show.active_state == "failed":
        exit_code = show.exec_main_status
        termination = termination_from_show(show.result, exit_code, show.exec_main_code)
        return ParsedUnitObservation(
            lifecycle_state=UnitLifecycleState.FAILED,
            owned=True,
            absence_proven=False,
            exit_code=exit_code,
            termination_class=termination,
        )
    if show.active_state in {"inactive", "dead"}:
        return _completed_observation(show)
    return ParsedUnitObservation(
        lifecycle_state=UnitLifecycleState.UNAVAILABLE,
        owned=False,
        absence_proven=False,
        exit_code=None,
        termination_class=None,
    )


def _completed_observation(show: SystemdUnitShow) -> ParsedUnitObservation:
    exit_code = show.exec_main_status
    termination = termination_from_show(show.result, exit_code, show.exec_main_code)
    lifecycle = (
        UnitLifecycleState.INACTIVE
        if termination == TerminationClass.SUCCESS
        else UnitLifecycleState.FAILED
    )
    return ParsedUnitObservation(
        lifecycle_state=lifecycle,
        owned=True,
        absence_proven=False,
        exit_code=exit_code,
        termination_class=termination,
    )


def termination_from_show(
    result: str | None,
    exit_code: int | None,
    exec_main_code: int | None,
) -> TerminationClass:
    if result == "timeout":
        return TerminationClass.TIMEOUT
    if result in {"signal", "core-dump"}:
        return TerminationClass.KILLED
    if exec_main_code == 2 and result in {None, "", "failed"}:
        return TerminationClass.TIMEOUT
    if exit_code == 0:
        return TerminationClass.SUCCESS
    if exit_code is None:
        return TerminationClass.UNKNOWN
    if exit_code > 0:
        return TerminationClass.NONZERO_EXIT
    return TerminationClass.KILLED

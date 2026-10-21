from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from ai_dev_loop.dashboard.adapters.http import (
    HTTP_CONTENT_CHUNK_BYTES,
    DashboardHttpDispatcher,
    HttpServiceFactories,
    decode_http_read_request,
)
from ai_dev_loop.integration_api.errors import IntegrationApiError


@dataclass(frozen=True)
class _Wire:
    value: dict[str, Any]

    def model_dump(self, *, mode: str, by_alias: bool) -> dict[str, Any]:
        assert mode == "json" and by_alias is True
        return self.value


@dataclass
class _Recorder:
    factories: list[str] = field(default_factory=list)
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = field(default_factory=list)

    def result(self, name: str, *args: object, **kwargs: object) -> _Wire:
        self.calls.append((name, args, kwargs))
        return _Wire({"operation": name})


class _DashboardService:
    def __init__(self, recorder: _Recorder) -> None:
        self.recorder = recorder

    def projects(self, **kwargs: object) -> _Wire:
        return self.recorder.result("dashboard.projects", **kwargs)

    def summary(self, **kwargs: object) -> _Wire:
        return self.recorder.result("dashboard.summary", **kwargs)

    def runs(self, **kwargs: object) -> _Wire:
        return self.recorder.result("dashboard.runs", **kwargs)

    def activity(self, **kwargs: object) -> _Wire:
        return self.recorder.result("dashboard.activity", **kwargs)

    def token_usage(self, **kwargs: object) -> _Wire:
        return self.recorder.result("dashboard.token-usage", **kwargs)


class _RunService:
    def __init__(self, recorder: _Recorder) -> None:
        self.recorder = recorder

    def inspect_run(self, *args: object, **kwargs: object) -> _Wire:
        return self.recorder.result("run.inspect", *args, **kwargs)

    def list_timeline(self, *args: object, **kwargs: object) -> _Wire:
        return self.recorder.result("run.timeline", *args, **kwargs)

    def read_initial_prompt_chunk(self, *args: object, **kwargs: object) -> _Wire:
        return self.recorder.result("run.initial-prompt", *args, **kwargs)


class _SequenceService:
    def __init__(self, recorder: _Recorder) -> None:
        self.recorder = recorder

    def inspect_sequence(self, *args: object, **kwargs: object) -> _Wire:
        return self.recorder.result("sequence.inspect", *args, **kwargs)

    def list_phase_runs(self, *args: object, **kwargs: object) -> _Wire:
        return self.recorder.result("sequence.phase-runs", *args, **kwargs)


class _ReviewService:
    def __init__(self, recorder: _Recorder) -> None:
        self.recorder = recorder

    def inspect_review(self, *args: object, **kwargs: object) -> _Wire:
        return self.recorder.result("run.review", *args, **kwargs)

    def read_review_content(self, *args: object, **kwargs: object) -> _Wire:
        return self.recorder.result("run.review-content", *args, **kwargs)


class _OutputService:
    def __init__(self, recorder: _Recorder) -> None:
        self.recorder = recorder

    def read_process_output(self, *args: object, **kwargs: object) -> _Wire:
        return self.recorder.result("run.output", *args, **kwargs)


class _CapacityService:
    def __init__(self, recorder: _Recorder) -> None:
        self.recorder = recorder

    def observe(self) -> _Wire:
        return self.recorder.result("machine.codex-capacity")


def _dispatcher(recorder: _Recorder) -> DashboardHttpDispatcher:
    def factory(name: str, cls: type[Any]) -> Any:
        recorder.factories.append(name)
        return cls(recorder)

    return DashboardHttpDispatcher(
        HttpServiceFactories(
            dashboard=lambda: factory("dashboard", _DashboardService),
            run=lambda: factory("run", _RunService),
            sequence=lambda: factory("sequence", _SequenceService),
            review=lambda: factory("review", _ReviewService),
            process_output=lambda: factory("process_output", _OutputService),
            capacity=lambda: factory("capacity", _CapacityService),
        )
    )


@pytest.mark.parametrize(
    ("operation", "parameters", "factory"),
    [
        ("dashboard.projects", {"offset": 2, "limit": 7}, "dashboard"),
        (
            "dashboard.summary",
            {"fromUtc": "2026-01-01T00:00:00Z", "toUtc": "2026-01-02T00:00:00Z"},
            "dashboard",
        ),
        (
            "dashboard.runs",
            {
                "fromUtc": "2026-01-01T00:00:00Z",
                "toUtc": "2026-01-02T00:00:00Z",
                "projectName": "p",
            },
            "dashboard",
        ),
        (
            "dashboard.activity",
            {
                "fromUtc": "2026-01-01T00:00:00Z",
                "toUtc": "2026-01-02T00:00:00Z",
                "metric": "review-rounds",
            },
            "dashboard",
        ),
        (
            "dashboard.token-usage",
            {
                "fromUtc": "2026-01-01T00:00:00Z",
                "toUtc": "2026-01-02T00:00:00Z",
                "runId": None,
            },
            "dashboard",
        ),
        ("run.inspect", {"runId": "run-1"}, "run"),
        ("run.timeline", {"runId": "run-1"}, "run"),
        ("sequence.inspect", {"sequenceId": "sequence-1"}, "sequence"),
        (
            "sequence.phase-runs",
            {"sequenceId": "sequence-1", "ordinal": 1},
            "sequence",
        ),
        ("machine.codex-capacity", {}, "capacity"),
        ("run.initial-prompt", {"runId": "run-1"}, "run"),
        ("run.review", {"runId": "run-1", "attemptId": "attempt-1"}, "review"),
        (
            "run.review-content",
            {
                "runId": "run-1",
                "attemptId": "attempt-1",
                "kind": "prompt",
            },
            "review",
        ),
        (
            "run.output",
            {"runId": "run-1", "attemptId": "attempt-1", "stream": "stdout"},
            "process_output",
        ),
    ],
)
def test_c262_2_allowlisted_dispatch_uses_one_typed_service(
    operation: str,
    parameters: dict[str, object],
    factory: str,
) -> None:
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)
    prepared = dispatcher.prepare(operation, parameters)
    assert recorder.factories == [] and recorder.calls == []
    assert prepared.capacity_probe is (operation == "machine.codex-capacity")
    assert prepared.execute() == {"operation": operation}
    assert recorder.factories == [factory]
    assert [call[0] for call in recorder.calls] == [operation]


@pytest.mark.parametrize(
    ("operation", "parameters"),
    [
        ("filesystem.read", {"path": "/private"}),
        ("run.output", {"runId": "r", "attemptId": "a", "stream": "combined"}),
        (
            "run.review-content",
            {"runId": "r", "attemptId": "a", "kind": "implementer-prompt"},
        ),
        ("sequence.phase-runs", {"sequenceId": "s", "ordinal": True}),
        ("dashboard.projects", {"limit": 101}),
        (
            "dashboard.token-usage",
            {"fromUtc": "2026-01-01T00:00:00Z", "sql": "select 1"},
        ),
        ("dashboard.token-usage", {"fromUtc": "2026-01-01T00:00:00Z", "runId": ""}),
        ("dashboard.token-usage", {"fromUtc": "2026-01-01T00:00:00Z", "runId": "../run"}),
        ("dashboard.projects", {"offset": False}),
        (
            "run.initial-prompt",
            {"runId": "r", "limit": HTTP_CONTENT_CHUNK_BYTES + 1},
        ),
    ],
)
def test_c262_2_invalid_parameters_fail_before_service_construction(
    operation: str,
    parameters: dict[str, object],
) -> None:
    recorder = _Recorder()
    with pytest.raises(IntegrationApiError) as failure:
        _dispatcher(recorder).prepare(operation, parameters)
    assert failure.value.code == "INVALID_ARGUMENT"
    assert recorder.factories == [] and recorder.calls == []


def test_c262_2_request_parser_rejects_ambiguous_or_non_json_bodies() -> None:
    valid = json.dumps({"operation": "dashboard.projects", "parameters": {}}).encode()
    assert decode_http_read_request(valid) == ("dashboard.projects", {})
    bad_bodies = (
        b'{"operation":"a","operation":"b","parameters":{}}',
        b'{"operation":"a","parameters":{},"path":"/private"}',
        b'{"operation":"a","parameters":NaN}',
        b'{"operation":"a","parameters":{"text":"\\ud800"}}',
        b"[]",
        b"\xff",
    )
    for body in bad_bodies:
        with pytest.raises(IntegrationApiError) as failure:
            decode_http_read_request(body)
        assert failure.value.code == "INVALID_ARGUMENT"

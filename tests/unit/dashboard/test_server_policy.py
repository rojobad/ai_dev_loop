from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import pytest

from ai_dev_loop.dashboard.adapters.http import (
    DashboardHttpDispatcher,
    HttpServiceDeadlineExceeded,
    HttpServiceFactories,
)
from ai_dev_loop.dashboard.application.ports import (
    DashboardObservationSet,
    DashboardReadScope,
    ProjectCatalogObservation,
    ProjectCountObservation,
)
from ai_dev_loop.dashboard.application.queries import DashboardQueries
from ai_dev_loop.dashboard.server import DashboardServer
from ai_dev_loop.integration_api.dashboard_service import IntegrationDashboardReadService


@dataclass(frozen=True)
class _Wire:
    payload: dict[str, Any]

    def model_dump(self, *, mode: str, by_alias: bool) -> dict[str, Any]:
        assert mode == "json" and by_alias is True
        return self.payload


class _ProjectsService:
    def __init__(
        self,
        *,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
        count: list[int] | None = None,
        failure: BaseException | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.started = started
        self.release = release
        self.count = count
        self.failure = failure
        self.payload = payload

    def projects(self, **kwargs: object) -> _Wire:
        if self.count is not None:
            self.count[0] += 1
            if self.count[0] >= 2 and self.started is not None:
                self.started.set()
        if self.release is not None:
            self.release.wait(3)
        if self.failure is not None:
            raise self.failure
        return _Wire(self.payload or {"items": [], "page": kwargs})


class _BlockingReadRepository:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release
        self.count = 0

    def read_project_catalog(self, *, offset: int, limit: int) -> ProjectCatalogObservation:
        self.count += 1
        self.started.set()
        self.release.wait(3)
        return ProjectCatalogObservation(
            items=(ProjectCountObservation(project_name="project", run_count=1),),
            total_count=1,
        )

    def read_observations(self, scope: DashboardReadScope) -> DashboardObservationSet:
        raise AssertionError(f"unexpected observation query: {scope.operation}")


class _BlockingCapacityService:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release
        self.count = 0

    def observe(self) -> _Wire:
        self.count += 1
        self.started.set()
        self.release.wait(3)
        return _Wire({"status": "available", "reason": None, "limits": []})


def _factories(projects: Any) -> HttpServiceFactories:
    def unused() -> Any:
        raise AssertionError("unexpected service factory")

    return HttpServiceFactories(
        dashboard=lambda: projects,  # type: ignore[arg-type]
        run=unused,
        sequence=unused,
        review=unused,
        process_output=unused,
        capacity=unused,
    )


def _start(
    projects: Any,
    *,
    handler_deadline: float = 30,
    socket_timeout: float = 5,
) -> tuple[DashboardServer, threading.Thread]:
    server = DashboardServer(
        0,
        dispatcher=DashboardHttpDispatcher(_factories(projects)),
        handler_deadline_seconds=handler_deadline,
        socket_timeout_seconds=socket_timeout,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    request_headers = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    result = response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    connection.close()
    return result


def _post(port: int, payload: dict[str, object]) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(payload).encode()
    return _request(
        port,
        "POST",
        "/api/dashboard/read",
        body=body,
        headers={
            "Origin": f"http://127.0.0.1:{port}",
            "Content-Type": "application/json",
        },
    )


def _wait_for(predicate: Any, *, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before the test deadline")
        time.sleep(0.01)


def _trickled_prefix(phase: str, port: int) -> bytes:
    if phase == "request-line":
        return b"POST /api/dashboard/read HTTP/1.1"
    if phase == "headers":
        return (
            b"POST /api/dashboard/read HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{port}\r\n".encode()
            + b"X-Trickle:"
        )
    assert phase == "body"
    return (
        b"POST /api/dashboard/read HTTP/1.1\r\n"
        + f"Host: 127.0.0.1:{port}\r\n".encode()
        + f"Origin: http://127.0.0.1:{port}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + b"Content-Length: 512\r\n"
        + b"Connection: close\r\n\r\n"
    )


def test_c262_4_manifest_headers_traversal_and_safe_internal_error() -> None:
    sentinel = "PRIVATE_SENTINEL /home/operator/artifacts/raw.jsonl"
    server, thread = _start(_ProjectsService(failure=RuntimeError(sentinel)))
    try:
        status, headers, body = _request(server.port, "GET", "/")
        assert status == 200 and b"/assets/dashboard.js" in body
        assert headers["cache-control"] == "no-store"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert "unsafe-eval" not in headers["content-security-policy"]
        assert "access-control-allow-origin" not in headers

        status, headers, body = _request(server.port, "GET", "/assets/dashboard.js")
        assert status == 200 and headers["content-type"].startswith("application/javascript")
        assert b"Project overview" in body

        status, headers, body = _request(server.port, "GET", "/assets/dashboard.css")
        assert status == 200 and headers["content-type"].startswith("text/css")
        assert b".app-shell" in body

        for path in ("/unlisted.txt", "/../private", "/%2e%2e/private", "/index.html?path=x"):
            status, _, body = _request(server.port, "GET", path)
            assert status == 404 and json.loads(body)["error"]["code"] == "NOT_FOUND"
            assert sentinel.encode() not in body

        status, _, body = _post(
            server.port,
            {"operation": "dashboard.projects", "parameters": {}},
        )
        assert status == 500
        assert json.loads(body)["error"] == {
            "code": "INTERNAL_ERROR",
            "message": "An internal error occurred.",
        }
        assert sentinel.encode() not in body
    finally:
        server.shutdown()
        thread.join(timeout=3)

    oversized, thread = _start(_ProjectsService(payload={"private": "x" * (241 * 1024)}))
    try:
        status, _, body = _post(
            oversized.port,
            {"operation": "dashboard.projects", "parameters": {}},
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == "INVALID_ARGUMENT"
        assert b"xxxxxxxxxxxxxxxx" not in body
    finally:
        oversized.shutdown()
        thread.join(timeout=3)


def test_c262_2_host_origin_method_and_size_rejections_dispatch_nothing() -> None:
    calls = [0]
    server, thread = _start(_ProjectsService(count=calls))
    valid = json.dumps({"operation": "dashboard.projects", "parameters": {}}).encode()
    try:
        cases = [
            _request(
                server.port,
                "POST",
                "/api/dashboard/read",
                body=valid,
                headers={"Content-Type": "application/json"},
            ),
            _request(
                server.port,
                "POST",
                "/api/dashboard/read",
                body=valid,
                headers={
                    "Host": "localhost:8765",
                    "Origin": f"http://127.0.0.1:{server.port}",
                    "Content-Type": "application/json",
                },
            ),
            _request(server.port, "PUT", "/api/dashboard/read", body=valid),
            _request(server.port, "CONNECT", "example.invalid:443"),
            _request(
                server.port,
                "POST",
                "/api/dashboard/read",
                body=b"x" * (16 * 1024 + 1),
                headers={
                    "Origin": f"http://127.0.0.1:{server.port}",
                    "Content-Type": "application/json",
                },
            ),
        ]
        assert [case[0] for case in cases] == [403, 403, 405, 405, 413]
        assert all(json.loads(case[2])["error"]["code"] == "INVALID_ARGUMENT" for case in cases)
        assert calls == [0]
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_c262_3_service_and_handler_admission_timeout_then_recovery() -> None:
    started = threading.Event()
    release = threading.Event()
    repository = _BlockingReadRepository(started, release)
    projects = IntegrationDashboardReadService(DashboardQueries(repository))
    server, thread = _start(projects, handler_deadline=0.8, socket_timeout=1)
    responses: list[int] = []

    def request() -> None:
        responses.append(
            _post(server.port, {"operation": "dashboard.projects", "parameters": {}})[0]
        )

    workers = [threading.Thread(target=request) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        assert started.wait(2)
        _wait_for(lambda: server.httpd.active_service_count == 2)
        saturated = _post(
            server.port,
            {"operation": "dashboard.projects", "parameters": {}},
        )
        assert saturated[0] == 503
        assert server.httpd.active_service_count == 2
        for worker in workers:
            worker.join(timeout=2)
        assert sorted(responses) == [503, 503]
        assert server.httpd.active_service_count == 2
        release.set()
        _wait_for(lambda: server.httpd.active_service_count == 0)

        idle = [socket.create_connection(("127.0.0.1", server.port), timeout=2) for _ in range(4)]
        _wait_for(lambda: server.httpd.active_handler_count == 4)
        fifth = socket.create_connection(("127.0.0.1", server.port), timeout=2)
        assert b" 503 " in fifth.recv(4096)
        fifth.close()
        for connection in idle:
            connection.close()
        _wait_for(lambda: server.httpd.active_handler_count == 0)

        status, _, body = _post(
            server.port,
            {"operation": "dashboard.projects", "parameters": {}},
        )
        assert status == 200 and json.loads(body)["ok"] is True
        assert server.httpd.active_service_count <= 2
        assert server.httpd.active_handler_count <= 4
        assert repository.count == 3
    finally:
        release.set()
        server.shutdown()
        thread.join(timeout=3)


@pytest.mark.parametrize("phase", ["request-line", "headers", "body"])
def test_c262_3_absolute_deadline_retires_four_trickled_handlers_and_recovers(
    phase: str,
) -> None:
    calls = [0]
    server, thread = _start(
        _ProjectsService(count=calls),
        handler_deadline=0.35,
        socket_timeout=1.5,
    )
    clients: list[socket.socket] = []
    stop_trickling = threading.Event()
    trickler: threading.Thread | None = None
    try:
        prefix = _trickled_prefix(phase, server.port)
        for _ in range(4):
            client = socket.create_connection(("127.0.0.1", server.port), timeout=2)
            client.sendall(prefix)
            clients.append(client)
        _wait_for(lambda: server.httpd.active_handler_count == 4)

        def trickle() -> None:
            while not stop_trickling.wait(0.02):
                for client in clients:
                    with suppress(OSError):
                        client.sendall(b"x")

        trickler = threading.Thread(target=trickle, daemon=True)
        started_at = time.monotonic()
        trickler.start()
        _wait_for(lambda: server.httpd.active_handler_count == 0, timeout=1)
        assert time.monotonic() - started_at < 0.8
        assert trickler.is_alive()
        assert calls == [0]

        status, _, payload = _post(
            server.port,
            {"operation": "dashboard.projects", "parameters": {}},
        )
        assert status == 200 and json.loads(payload)["ok"] is True
        assert calls == [1]
        assert server.httpd.active_handler_count <= 4
        assert server.httpd.active_service_count <= 2
    finally:
        stop_trickling.set()
        if trickler is not None:
            trickler.join(timeout=1)
        for client in clients:
            client.close()
        server.shutdown()
        thread.join(timeout=3)


@pytest.mark.parametrize("capacity_probe", [False, True])
def test_c262_3_expired_and_late_started_work_never_executes_and_permits_recover(
    capacity_probe: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = DashboardServer(0, dispatcher=DashboardHttpDispatcher(_factories(_ProjectsService())))
    calls = [0]

    def work() -> dict[str, Any]:
        calls[0] += 1
        return {"call": calls[0]}

    try:
        with pytest.raises(HttpServiceDeadlineExceeded):
            server.httpd.run_service(
                work,
                deadline=time.monotonic() - 1,
                capacity_probe=capacity_probe,
            )
        assert calls == [0]
        assert server.httpd.active_service_count == 0

        original_start = threading.Thread.start

        def start_after_deadline(worker: threading.Thread) -> None:
            time.sleep(0.08)
            original_start(worker)

        with monkeypatch.context() as delayed_start:
            delayed_start.setattr(threading.Thread, "start", start_after_deadline)
            with pytest.raises(HttpServiceDeadlineExceeded):
                server.httpd.run_service(
                    work,
                    deadline=time.monotonic() + 0.04,
                    capacity_probe=capacity_probe,
                )
        _wait_for(lambda: server.httpd.active_service_count == 0)
        assert calls == [0]

        result = server.httpd.run_service(
            work,
            deadline=time.monotonic() + 1,
            capacity_probe=capacity_probe,
        )
        assert result == {"call": 1}
        _wait_for(lambda: server.httpd.active_service_count == 0)
    finally:
        server.close()


def test_c262_3_disconnected_real_reader_retires_and_later_read_succeeds() -> None:
    started = threading.Event()
    release = threading.Event()
    repository = _BlockingReadRepository(started, release)
    service = IntegrationDashboardReadService(DashboardQueries(repository))
    server, thread = _start(service, handler_deadline=2, socket_timeout=1)
    body = json.dumps({"operation": "dashboard.projects", "parameters": {}}).encode()
    request = (
        "POST /api/dashboard/read HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{server.port}\r\n"
        f"Origin: http://127.0.0.1:{server.port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body
    try:
        disconnected = socket.create_connection(("127.0.0.1", server.port), timeout=2)
        disconnected.sendall(request)
        disconnected.close()
        assert started.wait(2)
        assert server.httpd.active_service_count == 1
        release.set()
        _wait_for(lambda: server.httpd.active_service_count == 0)
        _wait_for(lambda: server.httpd.active_handler_count == 0)
        status, _, payload = _post(
            server.port,
            {"operation": "dashboard.projects", "parameters": {}},
        )
        assert status == 200 and json.loads(payload)["data"]["totalCount"] == 1
        assert repository.count == 2
    finally:
        release.set()
        server.shutdown()
        thread.join(timeout=3)


def test_c262_3_capacity_probe_is_single_flight_and_releases_its_slot() -> None:
    started = threading.Event()
    release = threading.Event()
    capacity = _BlockingCapacityService(started, release)

    def unused() -> Any:
        raise AssertionError("unexpected service factory")

    factories = HttpServiceFactories(
        dashboard=unused,
        run=unused,
        sequence=unused,
        review=unused,
        process_output=unused,
        capacity=lambda: capacity,  # type: ignore[arg-type]
    )
    server = DashboardServer(0, dispatcher=DashboardHttpDispatcher(factories))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    responses: list[int] = []
    first = threading.Thread(
        target=lambda: responses.append(
            _post(
                server.port,
                {"operation": "machine.codex-capacity", "parameters": {}},
            )[0]
        )
    )
    try:
        first.start()
        assert started.wait(2)
        assert server.httpd.active_service_count == 1
        second = _post(
            server.port,
            {"operation": "machine.codex-capacity", "parameters": {}},
        )
        assert second[0] == 503
        assert server.httpd.active_service_count == 1
        release.set()
        first.join(timeout=3)
        assert responses == [200]
        _wait_for(lambda: server.httpd.active_service_count == 0)
        later = _post(
            server.port,
            {"operation": "machine.codex-capacity", "parameters": {}},
        )
        assert later[0] == 200 and capacity.count == 2
    finally:
        release.set()
        server.shutdown()
        thread.join(timeout=3)

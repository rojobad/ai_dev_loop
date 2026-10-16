"""Bounded loopback-only HTTP server lifecycle for the local dashboard."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from http import HTTPStatus
from http.server import HTTPServer
from typing import Any

from ai_dev_loop.dashboard.adapters.http import (
    HTTP_HANDLER_DEADLINE_SECONDS,
    HTTP_SOCKET_TIMEOUT_SECONDS,
    DashboardHttpDispatcher,
    DashboardRequestHandler,
    HttpServiceDeadlineExceeded,
    HttpServiceUnavailable,
    _json_bytes,
)
from ai_dev_loop.integration_api.envelope import build_failure_envelope
from ai_dev_loop.integration_api.errors import IntegrationApiError

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_DEFAULT_PORT = 8765
DASHBOARD_MAX_HANDLERS = 4
DASHBOARD_MAX_SERVICE_JOBS = 2


class BoundedDashboardHTTPServer(HTTPServer):
    """HTTPServer that admits a fixed number of handlers before thread creation."""

    allow_reuse_address = True
    request_queue_size = DASHBOARD_MAX_HANDLERS

    def __init__(
        self,
        port: int,
        *,
        dispatcher: DashboardHttpDispatcher | None = None,
        handler_deadline_seconds: float = HTTP_HANDLER_DEADLINE_SECONDS,
        socket_timeout_seconds: float = HTTP_SOCKET_TIMEOUT_SECONDS,
    ) -> None:
        self.dispatcher = dispatcher or DashboardHttpDispatcher()
        self.handler_deadline_seconds = handler_deadline_seconds
        self.socket_timeout_seconds = socket_timeout_seconds
        self._handler_slots = threading.BoundedSemaphore(DASHBOARD_MAX_HANDLERS)
        self._service_slots = threading.BoundedSemaphore(DASHBOARD_MAX_SERVICE_JOBS)
        self._capacity_slot = threading.BoundedSemaphore(1)
        self._lock = threading.Lock()
        self._handler_threads: set[threading.Thread] = set()
        self._handler_sockets: set[socket.socket] = set()
        self._service_threads: set[threading.Thread] = set()
        self._stopping = threading.Event()
        super().__init__(
            (DASHBOARD_HOST, port),
            DashboardRequestHandler,
            bind_and_activate=False,
        )
        try:
            self.server_bind()
            self.server_activate()
        except BaseException:
            self.server_close()
            raise
        host, bound_port = self.server_address[:2]
        if host != DASHBOARD_HOST:
            self.server_close()
            raise OSError("Dashboard server did not bind to the loopback address.")
        self.bound_port = int(bound_port)

    @property
    def active_handler_count(self) -> int:
        with self._lock:
            return len(self._handler_threads)

    @property
    def active_service_count(self) -> int:
        with self._lock:
            return len(self._service_threads)

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: Any,
    ) -> None:
        if not isinstance(request, socket.socket):
            self.shutdown_request(request)
            return
        if self._stopping.is_set() or not self._handler_slots.acquire(blocking=False):
            self._send_saturation(request)
            self.shutdown_request(request)
            return
        thread = threading.Thread(
            target=self._process_request_thread,
            args=(request, client_address),
            name="ai-dev-loop-dashboard-handler",
            daemon=True,
        )
        with self._lock:
            self._handler_threads.add(thread)
            self._handler_sockets.add(request)
        try:
            thread.start()
        except BaseException:
            with self._lock:
                self._handler_threads.discard(thread)
                self._handler_sockets.discard(request)
            self._handler_slots.release()
            self.shutdown_request(request)
            raise

    def _process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except BaseException:
            pass
        finally:
            self.shutdown_request(request)
            with self._lock:
                self._handler_sockets.discard(request)
                self._handler_threads.discard(threading.current_thread())
            self._handler_slots.release()

    @staticmethod
    def _send_saturation(request: socket.socket) -> None:
        body = _json_bytes(
            build_failure_envelope(
                IntegrationApiError.internal().code,
                IntegrationApiError.internal().message,
            )
        )
        response = (
            f"HTTP/1.1 {HTTPStatus.SERVICE_UNAVAILABLE.value} "
            f"{HTTPStatus.SERVICE_UNAVAILABLE.phrase}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Content-Security-Policy: default-src 'self'; object-src 'none'; base-uri 'none'\r\n"
            "Referrer-Policy: no-referrer\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body
        try:
            request.settimeout(HTTP_SOCKET_TIMEOUT_SECONDS)
            request.sendall(response)
        except OSError:
            return

    def run_service(
        self,
        work: Callable[[], dict[str, Any]],
        *,
        deadline: float,
        capacity_probe: bool,
    ) -> dict[str, Any]:
        if time.monotonic() >= deadline:
            raise HttpServiceDeadlineExceeded
        if self._stopping.is_set() or not self._service_slots.acquire(blocking=False):
            raise HttpServiceUnavailable
        capacity_acquired = False
        if capacity_probe:
            capacity_acquired = self._capacity_slot.acquire(blocking=False)
            if not capacity_acquired:
                self._service_slots.release()
                raise HttpServiceUnavailable

        if time.monotonic() >= deadline:
            if capacity_acquired:
                self._capacity_slot.release()
            self._service_slots.release()
            raise HttpServiceDeadlineExceeded

        future: Future[dict[str, Any]] = Future()

        def run() -> None:
            try:
                if not future.set_running_or_notify_cancel():
                    return
                if time.monotonic() >= deadline:
                    future.set_exception(HttpServiceDeadlineExceeded())
                    return
                future.set_result(work())
            except BaseException as exc:
                future.set_exception(exc)
            finally:
                if capacity_acquired:
                    self._capacity_slot.release()
                self._service_slots.release()
                with self._lock:
                    self._service_threads.discard(threading.current_thread())

        thread = threading.Thread(
            target=run,
            name="ai-dev-loop-dashboard-reader",
            daemon=True,
        )
        with self._lock:
            self._service_threads.add(thread)
        if time.monotonic() >= deadline:
            with self._lock:
                self._service_threads.discard(thread)
            if capacity_acquired:
                self._capacity_slot.release()
            self._service_slots.release()
            raise HttpServiceDeadlineExceeded
        try:
            thread.start()
        except BaseException:
            with self._lock:
                self._service_threads.discard(thread)
            if capacity_acquired:
                self._capacity_slot.release()
            self._service_slots.release()
            raise

        remaining = max(0.0, deadline - time.monotonic())
        response_budget = min(1.0, remaining / 2)
        try:
            return future.result(timeout=remaining - response_budget)
        except TimeoutError as exc:
            raise HttpServiceDeadlineExceeded from exc

    def stop_owned_work(self, *, timeout: float = HTTP_HANDLER_DEADLINE_SECONDS) -> None:
        self._stopping.set()
        with self._lock:
            sockets = tuple(self._handler_sockets)
        for request in sockets:
            with suppress(OSError):
                request.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                request.close()
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                threads = tuple(self._service_threads | self._handler_threads)
            if not threads:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            for thread in threads:
                thread.join(timeout=min(remaining, 0.1))

    def server_close(self) -> None:
        self._stopping.set()
        super().server_close()


class DashboardServer:
    """Owned loopback server with explicit start/stop lifecycle."""

    def __init__(
        self,
        port: int,
        *,
        dispatcher: DashboardHttpDispatcher | None = None,
        handler_deadline_seconds: float = HTTP_HANDLER_DEADLINE_SECONDS,
        socket_timeout_seconds: float = HTTP_SOCKET_TIMEOUT_SECONDS,
    ) -> None:
        self.httpd = BoundedDashboardHTTPServer(
            port,
            dispatcher=dispatcher,
            handler_deadline_seconds=handler_deadline_seconds,
            socket_timeout_seconds=socket_timeout_seconds,
        )

    @property
    def port(self) -> int:
        return self.httpd.bound_port

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.1)

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd.stop_owned_work()

    def close(self) -> None:
        self.httpd.server_close()
        self.httpd.stop_owned_work()

    def __enter__(self) -> DashboardServer:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

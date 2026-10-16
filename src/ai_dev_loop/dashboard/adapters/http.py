"""Typed, bounded HTTP projection over the local Integration API readers."""

from __future__ import annotations

import io
import json
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from importlib import resources
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from ai_dev_loop.integration_api.capacity_service import (
    CodexCapacityReadService,
    default_capacity_read_service,
)
from ai_dev_loop.integration_api.dashboard_service import (
    IntegrationDashboardReadService,
    default_dashboard_read_service,
)
from ai_dev_loop.integration_api.envelope import build_failure_envelope, build_success_envelope
from ai_dev_loop.integration_api.errors import IntegrationApiError, IntegrationErrorCode
from ai_dev_loop.integration_api.process_output_resolution import ProcessStreamName
from ai_dev_loop.integration_api.process_output_service import (
    IntegrationProcessOutputReadService,
    default_process_output_read_service,
)
from ai_dev_loop.integration_api.review_reader import ReviewContentKind
from ai_dev_loop.integration_api.review_service import (
    IntegrationReviewReadService,
    default_review_read_service,
)
from ai_dev_loop.integration_api.run_service import (
    IntegrationRunReadService,
    default_run_read_service,
)
from ai_dev_loop.integration_api.sequence_service import (
    IntegrationSequenceReadService,
    default_sequence_read_service,
)

HTTP_READ_PATH = "/api/dashboard/read"
HTTP_MAX_REQUEST_BYTES = 16 * 1024
HTTP_MAX_ENVELOPE_BYTES = 240 * 1024
HTTP_MAX_STATIC_BYTES = 10 * 1024 * 1024
HTTP_CONTENT_CHUNK_BYTES = 65_536
HTTP_DEFAULT_PAGE_LIMIT = 25
HTTP_MAX_PAGE_LIMIT = 100
HTTP_SOCKET_TIMEOUT_SECONDS = 5.0
HTTP_HANDLER_DEADLINE_SECONDS = 30.0

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


class HttpServiceUnavailable(RuntimeError):
    """The bounded local server cannot admit more read work."""


class HttpServiceDeadlineExceeded(TimeoutError):
    """The total request deadline elapsed while an owned read was running."""


class _DeadlineReader(io.RawIOBase):
    """Apply one absolute deadline to every raw socket receive."""

    def __init__(
        self,
        raw: io.RawIOBase,
        connection: socket.socket,
        *,
        deadline: float,
        idle_timeout: float,
    ) -> None:
        super().__init__()
        self._raw = raw
        self._connection = connection
        self._deadline = deadline
        self._idle_timeout = idle_timeout

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int | None:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("dashboard request deadline exceeded")
        self._connection.settimeout(min(self._idle_timeout, remaining))
        return self._raw.readinto(buffer)

    def close(self) -> None:
        if not self.closed:
            self._raw.close()
        super().close()


class _HttpServerPort(Protocol):
    bound_port: int
    dispatcher: DashboardHttpDispatcher
    handler_deadline_seconds: float
    socket_timeout_seconds: float

    def run_service(
        self,
        work: Callable[[], dict[str, Any]],
        *,
        deadline: float,
        capacity_probe: bool,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class StaticAsset:
    package: str
    name: str
    media_type: str

    def read(self) -> bytes:
        return resources.files(self.package).joinpath(self.name).read_bytes()


DEFAULT_STATIC_MANIFEST: Mapping[str, StaticAsset] = {
    "/": StaticAsset(
        package="ai_dev_loop.dashboard.web_dist",
        name="index.html",
        media_type="text/html; charset=utf-8",
    ),
    "/index.html": StaticAsset(
        package="ai_dev_loop.dashboard.web_dist",
        name="index.html",
        media_type="text/html; charset=utf-8",
    ),
}


@dataclass(frozen=True)
class HttpServiceFactories:
    dashboard: Callable[[], IntegrationDashboardReadService]
    run: Callable[[], IntegrationRunReadService]
    sequence: Callable[[], IntegrationSequenceReadService]
    review: Callable[[], IntegrationReviewReadService]
    process_output: Callable[[], IntegrationProcessOutputReadService]
    capacity: Callable[[], CodexCapacityReadService]


def default_http_service_factories(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> HttpServiceFactories:
    """Build readers lazily so bind/validation failures never touch storage."""

    return HttpServiceFactories(
        dashboard=lambda: default_dashboard_read_service(db_path=db_path),
        run=lambda: default_run_read_service(db_path=db_path, artifact_root=artifact_root),
        sequence=lambda: default_sequence_read_service(
            db_path=db_path,
            artifact_root=artifact_root,
        ),
        review=lambda: default_review_read_service(
            db_path=db_path,
            artifact_root=artifact_root,
        ),
        process_output=lambda: default_process_output_read_service(
            db_path=db_path,
            artifact_root=artifact_root,
        ),
        capacity=default_capacity_read_service,
    )


@dataclass(frozen=True)
class PreparedHttpRead:
    operation: str
    execute: Callable[[], dict[str, Any]]
    capacity_probe: bool = False


def _wire(model: Any) -> dict[str, Any]:
    payload = model.model_dump(mode="json", by_alias=True)
    if not isinstance(payload, dict):
        raise IntegrationApiError.internal()
    return cast(dict[str, Any], payload)


def _strict_int(
    parameters: Mapping[str, object],
    key: str,
    *,
    default: int | None = None,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    value = parameters.get(key, default)
    if type(value) is not int:
        raise IntegrationApiError.invalid_argument(f"{key} must be an integer.")
    result = value
    if result < minimum or (maximum is not None and result > maximum):
        if maximum is None:
            message = f"{key} must be at least {minimum}."
        else:
            message = f"{key} must be between {minimum} and {maximum}."
        raise IntegrationApiError.invalid_argument(message)
    return result


def _required_text(parameters: Mapping[str, object], key: str) -> str:
    value = parameters.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 1024:
        raise IntegrationApiError.invalid_argument(f"{key} must be a non-empty string.")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise IntegrationApiError.invalid_argument(f"{key} contains unsupported characters.")
    return value


def _dashboard_filter_kwargs(parameters: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {
        "from_value": parameters.get("fromUtc"),
        "to_value": parameters.get("toUtc"),
        "time_zone": parameters.get("timeZone", "America/La_Paz"),
    }
    if "projectName" in parameters:
        result["project_name"] = parameters["projectName"]
    return result


class DashboardHttpDispatcher:
    """Allowlisted operation-to-service dispatch with validation before I/O."""

    def __init__(self, factories: HttpServiceFactories | None = None) -> None:
        self._factories = factories or default_http_service_factories()

    def prepare(self, operation: object, parameters: object) -> PreparedHttpRead:
        if not isinstance(operation, str):
            raise IntegrationApiError.invalid_argument("operation must be a string.")
        if not isinstance(parameters, dict) or any(
            not isinstance(key, str) for key in parameters
        ):
            raise IntegrationApiError.invalid_argument("parameters must be an object.")
        params = cast(dict[str, object], parameters)

        if operation == "dashboard.projects":
            offset = _strict_int(params, "offset", default=0)
            limit = _strict_int(
                params,
                "limit",
                default=HTTP_DEFAULT_PAGE_LIMIT,
                minimum=1,
                maximum=HTTP_MAX_PAGE_LIMIT,
            )
            return PreparedHttpRead(
                operation,
                lambda: _wire(self._factories.dashboard().projects(offset=offset, limit=limit)),
            )

        if operation in {"dashboard.summary", "dashboard.runs", "dashboard.activity"}:
            kwargs = _dashboard_filter_kwargs(params)
            if operation == "dashboard.summary":
                return PreparedHttpRead(
                    operation,
                    lambda: _wire(self._factories.dashboard().summary(**kwargs)),
                )
            if operation == "dashboard.runs":
                kwargs["offset"] = _strict_int(params, "offset", default=0)
                kwargs["limit"] = _strict_int(
                    params,
                    "limit",
                    default=HTTP_DEFAULT_PAGE_LIMIT,
                    minimum=1,
                    maximum=HTTP_MAX_PAGE_LIMIT,
                )
                return PreparedHttpRead(
                    operation,
                    lambda: _wire(self._factories.dashboard().runs(**kwargs)),
                )
            kwargs["metric"] = params.get("metric")
            return PreparedHttpRead(
                operation,
                lambda: _wire(self._factories.dashboard().activity(**kwargs)),
            )

        if operation == "run.inspect":
            run_id = _required_text(params, "runId")
            return PreparedHttpRead(
                operation,
                lambda: _wire(self._factories.run().inspect_run(run_id)),
            )

        if operation == "run.timeline":
            run_id = _required_text(params, "runId")
            offset = _strict_int(params, "offset", default=0)
            limit = _strict_int(
                params,
                "limit",
                default=HTTP_DEFAULT_PAGE_LIMIT,
                minimum=1,
                maximum=HTTP_MAX_PAGE_LIMIT,
            )
            return PreparedHttpRead(
                operation,
                lambda: _wire(
                    self._factories.run().list_timeline(run_id, offset=offset, limit=limit)
                ),
            )

        if operation == "sequence.inspect":
            sequence_id = _required_text(params, "sequenceId")
            return PreparedHttpRead(
                operation,
                lambda: _wire(self._factories.sequence().inspect_sequence(sequence_id)),
            )

        if operation == "sequence.phase-runs":
            sequence_id = _required_text(params, "sequenceId")
            ordinal = _strict_int(params, "ordinal", minimum=1)
            offset = _strict_int(params, "offset", default=0)
            limit = _strict_int(
                params,
                "limit",
                default=HTTP_DEFAULT_PAGE_LIMIT,
                minimum=1,
                maximum=HTTP_MAX_PAGE_LIMIT,
            )
            return PreparedHttpRead(
                operation,
                lambda: _wire(
                    self._factories.sequence().list_phase_runs(
                        sequence_id,
                        ordinal=ordinal,
                        offset=offset,
                        limit=limit,
                    )
                ),
            )

        if operation == "machine.codex-capacity":
            return PreparedHttpRead(
                operation,
                lambda: _wire(self._factories.capacity().observe()),
                capacity_probe=True,
            )

        if operation == "run.initial-prompt":
            run_id = _required_text(params, "runId")
            offset = _strict_int(params, "byteOffset", default=0)
            limit = _strict_int(
                params,
                "limit",
                default=HTTP_CONTENT_CHUNK_BYTES,
                minimum=1,
                maximum=HTTP_CONTENT_CHUNK_BYTES,
            )
            return PreparedHttpRead(
                operation,
                lambda: _wire(
                    self._factories.run().read_initial_prompt_chunk(
                        run_id,
                        byte_offset=offset,
                        limit=limit,
                    )
                ),
            )

        if operation == "run.review":
            run_id = _required_text(params, "runId")
            attempt_id = _required_text(params, "attemptId")
            return PreparedHttpRead(
                operation,
                lambda: _wire(
                    self._factories.review().inspect_review(run_id, attempt_id=attempt_id)
                ),
            )

        if operation == "run.review-content":
            run_id = _required_text(params, "runId")
            attempt_id = _required_text(params, "attemptId")
            kind = _required_text(params, "kind")
            if kind not in {"prompt", "response", "review-markdown", "cursor-fix-prompt"}:
                raise IntegrationApiError.invalid_argument("Unsupported review content kind.")
            offset = _strict_int(params, "byteOffset", default=0)
            limit = _strict_int(
                params,
                "limit",
                default=HTTP_CONTENT_CHUNK_BYTES,
                minimum=1,
                maximum=HTTP_CONTENT_CHUNK_BYTES,
            )
            return PreparedHttpRead(
                operation,
                lambda: _wire(
                    self._factories.review().read_review_content(
                        run_id,
                        attempt_id=attempt_id,
                        content_kind=cast(ReviewContentKind, kind),
                        byte_offset=offset,
                        limit=limit,
                    )
                ),
            )

        if operation == "run.output":
            run_id = _required_text(params, "runId")
            attempt_id = _required_text(params, "attemptId")
            stream = _required_text(params, "stream")
            if stream not in {"stdout", "stderr"}:
                raise IntegrationApiError.invalid_argument("Unsupported process output stream.")
            offset = _strict_int(params, "byteOffset", default=0)
            limit = _strict_int(
                params,
                "limit",
                default=HTTP_CONTENT_CHUNK_BYTES,
                minimum=1,
                maximum=HTTP_CONTENT_CHUNK_BYTES,
            )
            return PreparedHttpRead(
                operation,
                lambda: _wire(
                    self._factories.process_output().read_process_output(
                        run_id,
                        attempt_id=attempt_id,
                        stream=cast(ProcessStreamName, stream),
                        byte_offset=offset,
                        limit=limit,
                    )
                ),
            )

        raise IntegrationApiError.invalid_argument("Unsupported dashboard read operation.")

    def dispatch(self, operation: object, parameters: object) -> dict[str, Any]:
        return self.prepare(operation, parameters).execute()


def decode_http_read_request(body: bytes) -> tuple[object, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    def reject_constant(_: str) -> object:
        raise ValueError("non-finite JSON number")

    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise IntegrationApiError.invalid_argument("Request body must be valid JSON.") from exc

    def reject_surrogates(value: object) -> None:
        if isinstance(value, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
                raise IntegrationApiError.invalid_argument(
                    "Request body contains invalid Unicode text."
                )
            return
        if isinstance(value, list):
            for item in value:
                reject_surrogates(item)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                reject_surrogates(key)
                reject_surrogates(item)

    reject_surrogates(payload)
    if not isinstance(payload, dict) or set(payload) != {"operation", "parameters"}:
        raise IntegrationApiError.invalid_argument(
            "Request body must contain operation and parameters."
        )
    return payload["operation"], payload["parameters"]


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _status_for_error(error: IntegrationApiError) -> HTTPStatus:
    if error.code is IntegrationErrorCode.INVALID_ARGUMENT:
        return HTTPStatus.BAD_REQUEST
    if error.code is IntegrationErrorCode.NOT_FOUND:
        return HTTPStatus.NOT_FOUND
    if error.code in {IntegrationErrorCode.UNSUPPORTED, IntegrationErrorCode.DATA_INTEGRITY}:
        return HTTPStatus.CONFLICT
    return HTTPStatus.INTERNAL_SERVER_ERROR


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Same-origin request adapter with no filesystem or command routing."""

    protocol_version = "HTTP/1.1"
    server_version = "ai_dev_loop-dashboard"
    sys_version = ""
    static_manifest: Mapping[str, StaticAsset] = DEFAULT_STATIC_MANIFEST

    def setup(self) -> None:
        self._request_started_at = time.monotonic()
        super().setup()
        server = cast(_HttpServerPort, self.server)
        self._request_deadline = self._request_started_at + server.handler_deadline_seconds
        self.connection.settimeout(server.socket_timeout_seconds)
        buffered_reader = cast(io.BufferedReader, self.rfile)
        raw_reader = buffered_reader.detach()
        self.rfile = io.BufferedReader(
            _DeadlineReader(
                raw_reader,
                self.connection,
                deadline=self._request_deadline,
                idle_timeout=server.socket_timeout_seconds,
            )
        )

    def log_message(self, format: str, *args: object) -> None:
        # Request targets, parameters, bodies and upstream diagnostics are intentionally omitted.
        return

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        return

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        # BaseHTTPRequestHandler otherwise emits an HTML page containing parser details.
        if code == HTTPStatus.NOT_IMPLEMENTED:
            self._reject_method()
            return
        if code in {HTTPStatus.REQUEST_URI_TOO_LONG, HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE}:
            self._send_error_envelope(
                IntegrationApiError.invalid_argument("HTTP request headers are too large."),
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        self._send_error_envelope(
            IntegrationApiError.invalid_argument("Invalid HTTP request."),
            status=HTTPStatus.BAD_REQUEST,
        )

    def handle_expect_100(self) -> bool:
        self._send_error_envelope(
            IntegrationApiError.invalid_argument("Expect request handling is unsupported."),
            status=HTTPStatus.BAD_REQUEST,
        )
        return False

    def _deadline(self) -> float:
        return self._request_deadline

    def _set_deadline_socket_timeout(self) -> bool:
        remaining = self._deadline() - time.monotonic()
        if remaining <= 0:
            return False
        server = cast(_HttpServerPort, self.server)
        self.connection.settimeout(min(server.socket_timeout_seconds, remaining))
        return True

    def _host_is_valid(self) -> bool:
        server = cast(_HttpServerPort, self.server)
        values = self.headers.get_all("Host", failobj=[])
        return values == [f"127.0.0.1:{server.bound_port}"]

    def _origin_is_valid(self) -> bool:
        server = cast(_HttpServerPort, self.server)
        values = self.headers.get_all("Origin", failobj=[])
        return values == [f"http://127.0.0.1:{server.bound_port}"]

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        media_type: str,
        include_body: bool = True,
    ) -> None:
        self.close_connection = True
        try:
            if not self._set_deadline_socket_timeout():
                return
            self.send_response(int(status))
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in _SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if include_body:
                if not self._set_deadline_socket_timeout():
                    return
                self.wfile.write(body)
                self.wfile.flush()
        except OSError:
            return

    def _send_error_envelope(
        self,
        error: IntegrationApiError,
        *,
        status: HTTPStatus | None = None,
        include_body: bool = True,
    ) -> None:
        body = _json_bytes(build_failure_envelope(error.code, error.message))
        self._send_bytes(
            status or _status_for_error(error),
            body,
            media_type="application/json; charset=utf-8",
            include_body=include_body,
        )

    def _reject_method(self, *, include_body: bool = True) -> None:
        self._send_error_envelope(
            IntegrationApiError.invalid_argument("Unsupported HTTP method."),
            status=HTTPStatus.METHOD_NOT_ALLOWED,
            include_body=include_body,
        )

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_is_valid():
            self._send_error_envelope(
                IntegrationApiError.invalid_argument("Invalid Host header."),
                status=HTTPStatus.FORBIDDEN,
            )
            return
        try:
            target = urlsplit(self.path)
        except ValueError:
            self._send_error_envelope(
                IntegrationApiError.invalid_argument("Invalid HTTP request target.")
            )
            return
        asset = (
            None
            if target.query or target.fragment
            else self.static_manifest.get(target.path)
        )
        if asset is None:
            self._send_error_envelope(
                IntegrationApiError.not_found("Static asset not found."),
                status=HTTPStatus.NOT_FOUND,
            )
            return
        try:
            body = asset.read()
        except (FileNotFoundError, ModuleNotFoundError, OSError):
            self._send_error_envelope(IntegrationApiError.internal())
            return
        if len(body) > HTTP_MAX_STATIC_BYTES:
            self._send_error_envelope(IntegrationApiError.internal())
            return
        self._send_bytes(HTTPStatus.OK, body, media_type=asset.media_type)

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_is_valid():
            self._send_error_envelope(
                IntegrationApiError.invalid_argument("Invalid Host header."),
                status=HTTPStatus.FORBIDDEN,
            )
            return
        try:
            target = urlsplit(self.path)
        except ValueError:
            self._send_error_envelope(
                IntegrationApiError.invalid_argument("Invalid HTTP request target.")
            )
            return
        if target.path != HTTP_READ_PATH or target.query or target.fragment:
            self._send_error_envelope(IntegrationApiError.not_found("Read route not found."))
            return
        if not self._origin_is_valid():
            self._send_error_envelope(
                IntegrationApiError.invalid_argument("Invalid Origin header."),
                status=HTTPStatus.FORBIDDEN,
            )
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._send_error_envelope(
                IntegrationApiError.invalid_argument("Streaming request bodies are unsupported."),
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        content_lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(content_lengths) != 1:
            self._send_error_envelope(
                IntegrationApiError.invalid_argument("A single Content-Length is required.")
            )
            return
        content_length_text = content_lengths[0]
        if not content_length_text.isascii() or not content_length_text.isdigit():
            self._send_error_envelope(
                IntegrationApiError.invalid_argument("Content-Length must be an integer.")
            )
            return
        content_length = int(content_length_text, 10)
        if content_length > HTTP_MAX_REQUEST_BYTES:
            self._send_error_envelope(
                IntegrationApiError.invalid_argument("Request body exceeds 16384 bytes."),
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_error_envelope(
                IntegrationApiError.invalid_argument("Content-Type must be application/json.")
            )
            return
        try:
            body = self.rfile.read(content_length)
        except OSError:
            self._send_error_envelope(
                IntegrationApiError.invalid_argument("Request body could not be read.")
            )
            return
        if len(body) != content_length:
            self._send_error_envelope(
                IntegrationApiError.invalid_argument("Request body was incomplete.")
            )
            return
        server = cast(_HttpServerPort, self.server)
        try:
            operation, parameters = decode_http_read_request(body)
            prepared = server.dispatcher.prepare(operation, parameters)
            data = server.run_service(
                prepared.execute,
                deadline=self._deadline(),
                capacity_probe=prepared.capacity_probe,
            )
            encoded = _json_bytes(build_success_envelope(data))
            if len(encoded) > HTTP_MAX_ENVELOPE_BYTES:
                raise IntegrationApiError.invalid_argument(
                    "Dashboard result is too large; narrow the requested scope."
                )
        except IntegrationApiError as exc:
            self._send_error_envelope(exc)
            return
        except (HttpServiceUnavailable, HttpServiceDeadlineExceeded):
            self._send_error_envelope(
                IntegrationApiError.internal(),
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        except BaseException:
            self._send_error_envelope(IntegrationApiError.internal())
            return
        self._send_bytes(
            HTTPStatus.OK,
            encoded,
            media_type="application/json; charset=utf-8",
        )

    def do_HEAD(self) -> None:  # noqa: N802
        self._reject_method(include_body=False)

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_method()

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_method()

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_method()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._reject_method()

    def do_TRACE(self) -> None:  # noqa: N802
        self._reject_method()

    def do_CONNECT(self) -> None:  # noqa: N802
        self._reject_method()

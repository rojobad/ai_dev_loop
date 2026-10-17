"""Offline public-API contracts for the pinned Cursor SDK adapter."""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_dev_loop.config import CursorSdkSection
from ai_dev_loop.integrations.cursor_sdk.adapter import (
    CursorSdkAdapter,
    build_cursor_sdk_client_config,
)
from ai_dev_loop.integrations.cursor_sdk.port import CursorSdkErrorCode, CursorSdkUsage
from ai_dev_loop.paths import (
    CURSOR_CREDENTIAL_MAX_BYTES,
    CursorSdkPathError,
    cursor_conversation_key,
    cursor_credentials_dir,
    ensure_cursor_sdk_client_paths,
    read_cursor_credential,
)

SECRET = "phase25-secret-key-sentinel"


class FakeSdkError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        proto_error_code: str | None = None,
        retry_after: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.proto_error_code = proto_error_code
        self.retry_after = retry_after
        self.request_id = request_id


class AuthenticationError(FakeSdkError):
    pass


class RateLimitError(FakeSdkError):
    pass


class APITimeoutError(FakeSdkError):
    pass


class NetworkError(FakeSdkError):
    pass


class AgentNotFoundError(FakeSdkError):
    pass


class NotFoundError(FakeSdkError):
    pass


class AgentBusyError(FakeSdkError):
    pass


class InternalServerError(FakeSdkError):
    pass


class BadRequestError(FakeSdkError):
    pass


class ConfigurationError(FakeSdkError):
    pass


@dataclass
class ModelParameterValue:
    id: str
    value: str


@dataclass
class ModelSelection:
    id: str
    params: tuple[ModelParameterValue, ...]


@dataclass
class SandboxOptions:
    enabled: bool


@dataclass
class LocalAgentStoreConfig:
    type: str
    root_dir: str


@dataclass
class LocalAgentOptions:
    cwd: Path
    setting_sources: list[str]
    sandbox_options: SandboxOptions
    store: LocalAgentStoreConfig


@dataclass
class AgentOptions:
    model: ModelSelection
    api_key: str
    local: LocalAgentOptions


@dataclass
class SendOptions:
    model: ModelSelection


@dataclass
class FakeUsage:
    input_tokens: int = 10
    output_tokens: int = 20
    cache_read_tokens: int = 30
    cache_write_tokens: int = 40
    total_tokens: int = 100
    reasoning_tokens: int | None = 5


@dataclass
class FakeRunResult:
    status: str = "finished"
    result: str = "one terminal answer"
    usage: FakeUsage | None = None


class FakeRun:
    def __init__(
        self,
        run_id: str = "run-11111111-1111-1111-1111-111111111111",
        agent_id: str = "agent-11111111-1111-1111-1111-111111111111",
    ) -> None:
        self.id = run_id
        self.agent_id = agent_id
        self.status = "finished"
        self.result = "one terminal answer"
        self.usage = FakeUsage()
        self.stream_calls = 0
        self.wait_calls = 0
        self.observe_calls: list[str | None] = []
        self.supports_calls = 0
        self.cancel_calls = 0
        self.wait_result = FakeRunResult(usage=self.usage)

    def stream(self) -> list[object]:
        self.stream_calls += 1
        return [SimpleNamespace(type="assistant"), SimpleNamespace(type="usage")]

    def wait(self) -> FakeRunResult:
        self.wait_calls += 1
        return self.wait_result

    def observe(self, *, after_offset: str | None = None) -> list[object]:
        self.observe_calls.append(after_offset)
        return [SimpleNamespace(kind="sdk_message"), SimpleNamespace(kind="result")]

    def supports(self, operation: str) -> bool:
        del operation
        self.supports_calls += 1
        return False

    def cancel(self) -> None:
        self.cancel_calls += 1


class FakeAgent:
    def __init__(self, agent_id: str, run: FakeRun) -> None:
        self.agent_id = agent_id
        self.run = run
        self.send_calls: list[tuple[str, SendOptions]] = []
        self.send_error: Exception | None = None

    def send(self, prompt: str, options: SendOptions) -> FakeRun:
        self.send_calls.append((prompt, options))
        if self.send_error is not None:
            raise self.send_error
        return self.run


class FakePage:
    def __init__(self, runs: list[FakeRun]) -> None:
        self.items = runs
        self.next_cursor = "next-page"

    def __iter__(self) -> Any:
        return iter(self.items)


class FakeClient:
    def __init__(self) -> None:
        self.closed = False
        self.create_calls: list[AgentOptions] = []
        self.resume_calls: list[tuple[str, AgentOptions]] = []
        self.get_calls: list[tuple[str, dict[str, object]]] = []
        self.list_calls: list[tuple[str, dict[str, object]]] = []
        self.create_error: Exception | None = None
        self.ping_error: Exception | None = None
        self.run = FakeRun()
        self.expected_run_id = self.run.id
        self.agent = FakeAgent(self.run.agent_id, self.run)

    def ping(self) -> str:
        if self.ping_error is not None:
            raise self.ping_error
        return "pong"

    def get_version(self) -> dict[str, str]:
        return {"bridgeVersion": "1.0.0", "protocolVersion": "sdk.v1"}

    def close(self) -> None:
        self.closed = True

    def create_agent(self, options: AgentOptions) -> FakeAgent:
        self.create_calls.append(options)
        if self.create_error is not None:
            raise self.create_error
        return self.agent

    def resume_agent(self, agent_id: str, options: AgentOptions) -> FakeAgent:
        self.resume_calls.append((agent_id, options))
        if agent_id != self.agent.agent_id:
            raise AgentNotFoundError("missing")
        return self.agent

    def get_run(self, run_id: str, options: dict[str, object]) -> FakeRun:
        self.get_calls.append((run_id, options))
        if run_id != self.expected_run_id:
            raise NotFoundError("missing")
        return self.run

    def list_runs(self, agent_id: str, **options: object) -> FakePage:
        self.list_calls.append((agent_id, options))
        return FakePage([self.run])


class FakeCursorClient:
    client: FakeClient
    launch_error: Exception | None = None
    launch_calls: list[dict[str, object]] = []
    launch_environments: list[dict[str, str]] = []

    @classmethod
    def launch_bridge(cls, **kwargs: object) -> FakeClient:
        cls.launch_calls.append(kwargs)
        cls.launch_environments.append(dict(os.environ))
        if cls.launch_error is not None:
            raise cls.launch_error
        return cls.client


def _fake_sdk(client: FakeClient) -> SimpleNamespace:
    FakeCursorClient.client = client
    FakeCursorClient.launch_error = None
    FakeCursorClient.launch_calls = []
    FakeCursorClient.launch_environments = []
    return SimpleNamespace(
        __version__="1.0.37",
        CursorClient=FakeCursorClient,
        LocalAgentOptions=LocalAgentOptions,
        LocalAgentStoreConfig=LocalAgentStoreConfig,
        SandboxOptions=SandboxOptions,
        ModelSelection=ModelSelection,
        ModelParameterValue=ModelParameterValue,
        AgentOptions=AgentOptions,
        SendOptions=SendOptions,
        errors=SimpleNamespace(
            AuthenticationError=AuthenticationError,
            RateLimitError=RateLimitError,
            APITimeoutError=APITimeoutError,
            NetworkError=NetworkError,
            AgentNotFoundError=AgentNotFoundError,
            NotFoundError=NotFoundError,
            AgentBusyError=AgentBusyError,
            InternalServerError=InternalServerError,
            BadRequestError=BadRequestError,
            ConfigurationError=ConfigurationError,
        ),
    )


def _selection() -> CursorSdkSection:
    return CursorSdkSection.model_validate(
        {
            "runtime": "sdk-local",
            "sdk_version": "1.0.37",
            "model": "grok-4.7",
            "parameters": {
                "reasoning_effort": "high",
                "fast": False,
                "context": "256k",
            },
            "credential_ref": "cursor-default",
            "setting_sources": ["project"],
            "sandbox": "disabled",
        }
    )


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return path


def _credential(isolated_xdg: Path, value: str = SECRET) -> Path:
    app_config = _private_dir(isolated_xdg / "config" / "ai_dev_loop")
    credentials = _private_dir(app_config / "credentials")
    path = credentials / "cursor-default"
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def _adapter(
    git_repo: Path,
    isolated_xdg: Path,
    client: FakeClient,
) -> CursorSdkAdapter:
    _credential(isolated_xdg, SECRET + "\n")
    config = build_cursor_sdk_client_config(
        _selection(),
        workspace=git_repo,
        repository_root=git_repo,
        conversation_owner_identity="fixture-run-owner",
    )
    sdk = _fake_sdk(client)
    return CursorSdkAdapter(config, repository_root=git_repo, sdk_loader=lambda: sdk)


def test_credential_and_store_paths_are_private_confined_and_distinct(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    credential = _credential(isolated_xdg, SECRET + "\n")
    assert read_cursor_credential("cursor-default", repository_root=git_repo) == SECRET
    assert cursor_credentials_dir() == credential.parent
    first = ensure_cursor_sdk_client_paths(
        workspace=git_repo,
        repository_root=git_repo,
        conversation_owner_identity="owner-one",
    )
    second = ensure_cursor_sdk_client_paths(
        workspace=git_repo,
        repository_root=git_repo,
        conversation_owner_identity="owner-two",
    )
    assert first.conversation_key == cursor_conversation_key("owner-one")
    assert first.store_root != first.state_root
    assert first.store_root != second.store_root
    assert first.state_root != second.state_root
    assert first.store_root.parent.name == first.conversation_key
    assert oct(first.store_root.stat().st_mode & 0o777) == "0o700"


def test_credential_rejects_symlink_mode_multiline_and_repository_overlap(
    git_repo: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_config = _private_dir(isolated_xdg / "config" / "ai_dev_loop")
    _private_dir(app_config / "credentials")
    with pytest.raises(CursorSdkPathError, match="credential_missing"):
        read_cursor_credential("cursor-default", repository_root=git_repo)
    credential = _credential(isolated_xdg)
    credential.chmod(0o644)
    with pytest.raises(CursorSdkPathError, match="credential_insecure_permissions"):
        read_cursor_credential("cursor-default", repository_root=git_repo)
    credential.chmod(0o600)
    credential.write_text("", encoding="utf-8")
    with pytest.raises(CursorSdkPathError, match="credential_invalid_payload"):
        read_cursor_credential("cursor-default", repository_root=git_repo)
    credential.write_text("x" * (CURSOR_CREDENTIAL_MAX_BYTES + 1), encoding="utf-8")
    with pytest.raises(CursorSdkPathError, match="credential_too_large"):
        read_cursor_credential("cursor-default", repository_root=git_repo)
    credential.write_text(f"{SECRET}\nsecond-line\n", encoding="utf-8")
    with pytest.raises(CursorSdkPathError, match="credential_invalid_payload"):
        read_cursor_credential("cursor-default", repository_root=git_repo)
    credential.unlink()
    target = isolated_xdg / "outside-key"
    target.write_text(SECRET, encoding="utf-8")
    target.chmod(0o600)
    credential.symlink_to(target)
    with pytest.raises(CursorSdkPathError, match="credential_symlink"):
        read_cursor_credential("cursor-default", repository_root=git_repo)
    credential.unlink()
    credential.mkdir()
    with pytest.raises(CursorSdkPathError, match="credential_not_regular_file"):
        read_cursor_credential("cursor-default", repository_root=git_repo)
    monkeypatch.setenv("XDG_STATE_HOME", str(git_repo / ".state"))
    with pytest.raises(CursorSdkPathError, match="state_repository_overlap"):
        ensure_cursor_sdk_client_paths(
            workspace=git_repo,
            repository_root=git_repo,
            conversation_owner_identity="owner",
        )
    assert SECRET not in repr(CursorSdkPathError("credential_invalid_payload"))


def test_adapter_passes_exact_public_options_without_key_in_bridge_environment(
    git_repo: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeClient()
    adapter = _adapter(git_repo, isolated_xdg, client)
    monkeypatch.setenv("CURSOR_API_KEY", SECRET)
    monkeypatch.setenv("CURSOR_ACCESS_TOKEN", "different-secret")
    monkeypatch.setenv("UNRELATED_SECRET_COPY", SECRET)
    opened = adapter.open()
    assert opened.value is not None
    assert opened.value.bridge_version == "1.0.0"
    launch = FakeCursorClient.launch_calls[0]
    assert launch["timeout"] == 30
    assert launch["client_timeout"] == 60
    assert launch["max_retries"] == 0
    assert launch["allow_api_key_env_fallback"] is True
    assert SECRET not in FakeCursorClient.launch_environments[0].values()
    assert "CURSOR_ACCESS_TOKEN" not in FakeCursorClient.launch_environments[0]
    local = launch["local"]
    assert isinstance(local, LocalAgentOptions)
    assert local.setting_sources == ["project"]
    assert local.sandbox_options.enabled is False
    assert local.store.type == "jsonl"

    created = adapter.create_agent()
    assert created.value is not None
    create_options = client.create_calls[0]
    assert create_options.api_key == SECRET
    assert [(item.id, item.value) for item in create_options.model.params] == [
        ("context", "256k"),
        ("fast", "false"),
        ("reasoning_effort", "high"),
    ]
    sent = adapter.send(client.agent.agent_id, "implement only the approved scope")
    assert sent.value is not None
    send_options = client.agent.send_calls[0][1]
    assert [(item.id, item.value) for item in send_options.model.params] == [
        ("context", "256k"),
        ("fast", "false"),
        ("reasoning_effort", "high"),
    ]
    page = adapter.list_runs(client.agent.agent_id, cursor="offset", limit=5)
    assert page.value is not None and page.value.next_cursor == "next-page"
    assert client.list_calls[0][1] == {
        "runtime": "local",
        "cwd": git_repo.resolve(),
        "cursor": "offset",
        "limit": 5,
        "api_key": SECRET,
    }
    adapter.close()
    assert client.closed is True
    assert os.environ["CURSOR_API_KEY"] == SECRET
    assert os.environ["CURSOR_ACCESS_TOKEN"] == "different-secret"
    assert SECRET not in repr(opened)
    assert SECRET not in repr(created)
    assert SECRET not in repr(sent)
    assert SECRET not in caplog.text


def test_live_stream_combines_run_identity_with_terminal_result(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    client = FakeClient()
    adapter = _adapter(git_repo, isolated_xdg, client)
    assert adapter.open().value is not None
    assert adapter.create_agent().value is not None
    sent = adapter.send(client.agent.agent_id, "one turn")
    assert sent.value is not None
    terminal = adapter.stream(client.agent.agent_id, client.run.id)
    assert terminal.value is not None
    assert terminal.value.agent_id == client.agent.agent_id
    assert terminal.value.run_id == client.run.id
    assert terminal.value.text == "one terminal answer"
    assert terminal.value.usage is not None
    assert terminal.value.usage.total_tokens == 100
    assert client.run.stream_calls == 1
    adapter.close()


def test_terminal_result_redacts_only_the_active_credential(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    client = FakeClient()
    client.run.wait_result = FakeRunResult(
        result=f"ordinary prefix {SECRET} ordinary suffix",
        usage=FakeUsage(),
    )
    adapter = _adapter(git_repo, isolated_xdg, client)
    assert adapter.open().value is not None
    assert adapter.create_agent().value is not None
    sent = adapter.send(client.agent.agent_id, "one turn")
    assert sent.value is not None

    terminal = adapter.stream(client.agent.agent_id, client.run.id)

    assert terminal.value is not None
    assert terminal.value.text == "ordinary prefix [redacted] ordinary suffix"
    assert SECRET not in terminal.value.text
    adapter.close()


def test_public_replay_ignores_unreliable_supports_and_returns_one_terminal_usage(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    client = FakeClient()
    client.run.status = "running"
    client.run.result = "stale running snapshot"
    client.run.usage = None
    client.run.wait_result = FakeRunResult(
        status="finished",
        result="observed terminal answer",
        usage=FakeUsage(
            input_tokens=11,
            output_tokens=22,
            cache_read_tokens=33,
            cache_write_tokens=44,
            total_tokens=110,
            reasoning_tokens=6,
        ),
    )
    adapter = _adapter(git_repo, isolated_xdg, client)
    assert adapter.open().value is not None
    replay = adapter.stream(
        client.run.agent_id,
        client.run.id,
        mode="replay",
        after_offset="5",
    )
    assert replay.value is not None
    assert replay.value.status == "finished"
    assert replay.value.text == "observed terminal answer"
    assert replay.value.usage is not None
    assert replay.value.usage == CursorSdkUsage(
        input_tokens=11,
        output_tokens=22,
        cache_read_tokens=33,
        cache_write_tokens=44,
        total_tokens=110,
        reasoning_tokens=6,
    )
    assert client.run.observe_calls == ["5"]
    assert client.run.wait_calls == 1
    assert client.run.supports_calls == 0
    assert not hasattr(replay.value, "event_count")
    assert not hasattr(replay.value, "result_count")
    assert not hasattr(replay.value, "replay_count")
    assert client.get_calls == [
        (
            client.run.id,
            {
                "runtime": "local",
                "agentId": client.run.agent_id,
                "apiKey": SECRET,
            },
        )
    ]
    adapter.close()


def test_active_credential_is_removed_from_error_message_and_metadata(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    client = FakeClient()
    client.create_error = AuthenticationError(
        f"rejected credential {SECRET}",
        request_id=SECRET,
        retry_after=f"retry-{SECRET}",
    )
    adapter = _adapter(git_repo, isolated_xdg, client)
    assert adapter.open().value is not None
    outcome = adapter.create_agent()
    assert outcome.failure is not None
    assert outcome.failure.code is CursorSdkErrorCode.AUTHENTICATION_FAILED
    assert outcome.failure.request_id is None
    assert outcome.failure.retry_after is None
    assert SECRET not in repr(outcome)
    assert len(client.create_calls) == 1
    adapter.close()


def test_active_credential_is_removed_from_bridge_launch_failure_metadata(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    client = FakeClient()
    adapter = _adapter(git_repo, isolated_xdg, client)
    FakeCursorClient.launch_error = AuthenticationError(
        f"bridge rejected credential {SECRET}",
        request_id=SECRET,
        retry_after=SECRET,
    )
    outcome = adapter.open()
    assert outcome.failure is not None
    assert outcome.failure.code is CursorSdkErrorCode.AUTHENTICATION_FAILED
    assert outcome.failure.request_id is None
    assert outcome.failure.retry_after is None
    assert SECRET not in repr(outcome)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AuthenticationError("bad key"), CursorSdkErrorCode.AUTHENTICATION_FAILED),
        (
            RateLimitError(
                "capacity",
                retry_after="10",
                request_id="request-429",
            ),
            CursorSdkErrorCode.RATE_LIMITED,
        ),
        (APITimeoutError("timeout"), CursorSdkErrorCode.TIMEOUT),
        (AgentNotFoundError("missing"), CursorSdkErrorCode.UNKNOWN_IDENTITY),
        (
            InternalServerError(
                "internal: [feature_unavailable] account feature unavailable",
                code="internal",
            ),
            CursorSdkErrorCode.FEATURE_UNAVAILABLE,
        ),
        (
            InternalServerError("unclassified server error", code="internal"),
            CursorSdkErrorCode.SERVER_ERROR,
        ),
    ],
)
def test_failures_are_safe_typed_and_mutating_create_is_not_retried(
    git_repo: Path,
    isolated_xdg: Path,
    error: Exception,
    expected: CursorSdkErrorCode,
) -> None:
    client = FakeClient()
    client.create_error = error
    adapter = _adapter(git_repo, isolated_xdg, client)
    assert adapter.open().value is not None
    outcome = adapter.create_agent()
    assert outcome.failure is not None
    assert outcome.failure.code is expected
    assert outcome.failure.agent_id is None
    assert outcome.failure.run_id is None
    assert len(client.create_calls) == 1
    assert SECRET not in repr(outcome)
    if expected is CursorSdkErrorCode.RATE_LIMITED:
        assert outcome.failure.retry_after == "10"
        assert outcome.failure.request_id == "request-429"
    adapter.close()


def test_pinned_generic_busy_is_classified_only_in_send_context(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    client = FakeClient()
    adapter = _adapter(git_repo, isolated_xdg, client)
    assert adapter.open().value is not None
    assert adapter.create_agent().value is not None
    client.agent.send_error = InternalServerError(
        "internal: agent already has active run",
        code="internal",
    )
    outcome = adapter.send(client.agent.agent_id, "next turn")
    assert outcome.failure is not None
    assert outcome.failure.code is CursorSdkErrorCode.BUSY
    assert outcome.failure.agent_id == client.agent.agent_id
    assert len(client.agent.send_calls) == 1
    adapter.close()


def test_unknown_run_and_exact_cancel_use_registered_public_handle(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    client = FakeClient()
    adapter = _adapter(git_repo, isolated_xdg, client)
    assert adapter.open().value is not None
    missing = adapter.get_run(client.agent.agent_id, "run-missing")
    assert missing.failure is not None
    assert missing.failure.code is CursorSdkErrorCode.UNKNOWN_IDENTITY
    cancelled = adapter.cancel(client.run.agent_id, client.run.id)
    assert cancelled.value is not None
    assert client.run.cancel_calls == 1
    adapter.close()


@pytest.mark.parametrize(
    ("attribute", "mismatched"),
    [
        ("agent_id", "agent-22222222-2222-2222-2222-222222222222"),
        ("id", "run-22222222-2222-2222-2222-222222222222"),
    ],
)
def test_cancel_rejects_mismatched_returned_identity_without_request(
    git_repo: Path,
    isolated_xdg: Path,
    attribute: str,
    mismatched: str,
) -> None:
    client = FakeClient()
    requested_agent_id = client.run.agent_id
    requested_run_id = client.run.id
    setattr(client.run, attribute, mismatched)
    adapter = _adapter(git_repo, isolated_xdg, client)
    assert adapter.open().value is not None
    outcome = adapter.cancel(requested_agent_id, requested_run_id)
    assert outcome.failure is not None
    assert outcome.failure.code is CursorSdkErrorCode.INVALID_RESPONSE
    assert outcome.failure.agent_id == requested_agent_id
    assert outcome.failure.run_id == requested_run_id
    assert client.run.cancel_calls == 0
    adapter.close()


def test_failed_health_closes_bridge_and_adapter_uses_no_sdk_private_surface(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    client = FakeClient()
    client.ping_error = NetworkError("bridge unavailable")
    adapter = _adapter(git_repo, isolated_xdg, client)
    outcome = adapter.open()
    assert outcome.failure is not None
    assert outcome.failure.code is CursorSdkErrorCode.NETWORK
    assert client.closed is True
    source = inspect.getsource(CursorSdkAdapter)
    for private_name in ("_owned_bridge", "_endpoint", "_transport", "_agent_unary"):
        assert private_name not in source


def test_adapter_context_owns_bridge_on_normal_and_exceptional_exit(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    normal_client = FakeClient()
    with _adapter(git_repo, isolated_xdg, normal_client) as adapter:
        assert adapter.create_agent().value is not None
    assert normal_client.closed is True

    exceptional_client = FakeClient()
    with (
        pytest.raises(RuntimeError, match="worker failure"),
        _adapter(git_repo, isolated_xdg, exceptional_client),
    ):
        raise RuntimeError("worker failure")
    assert exceptional_client.closed is True

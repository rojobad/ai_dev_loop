"""Python 3.11 wheel/extra and keyless bundled-bridge acceptance."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, (
        f"command failed ({result.returncode}): {argv!r}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _isolated_env(root: Path) -> dict[str, str]:
    home = root / "home"
    xdg_config = root / "xdg-config"
    xdg_state = root / "xdg-state"
    xdg_cache = root / "xdg-cache"
    uv_cache = root / "uv-cache"
    temp = root / "tmp"
    for path in (home, xdg_config, xdg_state, xdg_cache, uv_cache, temp):
        path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_STATE_HOME": str(xdg_state),
            "XDG_CACHE_HOME": str(xdg_cache),
            "UV_CACHE_DIR": str(uv_cache),
            "TMPDIR": str(temp),
            "TMP": str(temp),
            "TEMP": str(temp),
        }
    )
    for name in tuple(env):
        if name.startswith("CURSOR_") and ("KEY" in name or "TOKEN" in name):
            env.pop(name)
    return env


def test_python311_built_extra_bridge_and_base_historical_imports(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    python311 = shutil.which("python3.11")
    uv = shutil.which("uv")
    if python311 is None or uv is None:
        raise AssertionError(
            "Phase 25.1 requires python3.11 and uv for isolated packaging validation"
        )
    env = _isolated_env(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    _run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
        cwd=repository,
        env=env,
    )
    wheels = list(dist.glob("ai_dev_loop-*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    with ZipFile(wheel) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith("METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
    assert "Provides-Extra: cursor-sdk" in metadata
    assert "Requires-Dist: cursor-sdk==1.0.37; extra == 'cursor-sdk'" in metadata

    extra_env = tmp_path / "extra-venv"
    _run([uv, "venv", "--python", python311, str(extra_env)], cwd=tmp_path, env=env)
    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(extra_env / "bin/python"),
            f"{wheel}[cursor-sdk]",
        ],
        cwd=tmp_path,
        env=env,
    )

    no_node_bin = tmp_path / "no-node-bin"
    no_node_bin.mkdir()
    (no_node_bin / "sh").symlink_to("/bin/sh")
    (no_node_bin / "dirname").symlink_to("/usr/bin/dirname")
    workspace = tmp_path / "workspace"
    native_state = tmp_path / "native-state"
    workspace.mkdir()
    native_state.mkdir()
    health_script = """
import importlib.metadata
import json
import os
import platform
import shutil
from pathlib import Path
from cursor_sdk import CursorClient, LocalAgentOptions, LocalAgentStoreConfig
from ai_dev_loop.config import CursorSdkSection
from ai_dev_loop.integrations.cursor_sdk.adapter import (
    CursorSdkAdapter,
    build_cursor_sdk_client_config,
)
root = Path.cwd()
assert shutil.which("node") is None
local = LocalAgentOptions(
    cwd=root / "workspace",
    store=LocalAgentStoreConfig(type="jsonl", root_dir=str(root / "native-state")),
)
with CursorClient.launch_bridge(
    workspace=root / "workspace",
    state_root=root / "native-state",
    local=local,
    timeout=30,
    client_timeout=30,
    max_retries=0,
    allow_api_key_env_fallback=True,
) as client:
    payload = {
        "ping": client.ping(),
        "bridge": client.get_version(),
    }
config_root = Path(os.environ["XDG_CONFIG_HOME"]) / "ai_dev_loop"
credential_root = config_root / "credentials"
credential_root.mkdir(parents=True)
config_root.chmod(0o700)
credential_root.chmod(0o700)
credential = credential_root / "fake-key"
credential.write_text("not-a-real-key", encoding="utf-8")
credential.chmod(0o600)
selection = CursorSdkSection.model_validate({
    "runtime": "sdk-local",
    "sdk_version": "1.0.37",
    "model": "offline-health-only",
    "parameters": {"fast": False},
    "credential_ref": "fake-key",
    "setting_sources": ["project"],
    "sandbox": "disabled",
})
adapter_config = build_cursor_sdk_client_config(
    selection,
    workspace=root / "workspace",
    repository_root=root / "workspace",
    conversation_owner_identity="packaging-health",
)
with CursorSdkAdapter(adapter_config, repository_root=root / "workspace"):
    pass
payload.update({
    "python_version": platform.python_version(),
    "sdk_version": importlib.metadata.version("cursor-sdk"),
    "external_node": shutil.which("node"),
    "adapter_lifecycle": "ok",
})
print(json.dumps(payload, sort_keys=True))
"""
    health_env = {
        "HOME": env["HOME"],
        "XDG_CONFIG_HOME": env["XDG_CONFIG_HOME"],
        "XDG_STATE_HOME": env["XDG_STATE_HOME"],
        "XDG_CACHE_HOME": env["XDG_CACHE_HOME"],
        "TMPDIR": env["TMPDIR"],
        "TMP": env["TMP"],
        "TEMP": env["TEMP"],
        "PATH": str(no_node_bin),
    }
    health = _run(
        [str(extra_env / "bin/python"), "-c", health_script],
        cwd=tmp_path,
        env=health_env,
        timeout=60,
    )
    payload = json.loads(health.stdout)
    python_version = payload.pop("python_version")
    assert python_version.startswith("3.11.")
    assert payload == {
        "bridge": {
            "bridgeVersion": "1.0.0",
            "capabilities": [
                "agent.create",
                "agent.resume",
                "agent.send",
                "run.observe",
                "run.wait",
                "run.cancel",
                "agent.management",
                "cursor.catalog",
                "artifacts.chunked",
                "agent.usage",
            ],
            "protocolVersion": "sdk.v1",
        },
        "adapter_lifecycle": "ok",
        "external_node": None,
        "ping": "pong",
        "sdk_version": "1.0.37",
    }

    correction_script = r"""
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import cursor_sdk
import cursor_sdk.errors as sdk_errors
import httpx

from ai_dev_loop.integrations.cursor_sdk.adapter import CursorSdkAdapter
from ai_dev_loop.integrations.cursor_sdk.port import (
    CursorSdkClientConfig,
    CursorSdkErrorCode,
    CursorSdkUsage,
)

root = Path.cwd()
workspace = root / "workspace"
secret = "not-a-real-key"
agent_id = "agent-11111111-1111-1111-1111-111111111111"
run_id = "run-11111111-1111-1111-1111-111111111111"
terminal = {
    "runId": run_id,
    "agentId": agent_id,
    "status": "finished",
    "result": "terminal result from public wait",
    "usage": {
        "inputTokens": 11,
        "outputTokens": 22,
        "cacheReadTokens": 33,
        "cacheWriteTokens": 44,
        "totalTokens": 110,
        "reasoningTokens": 6,
    },
}


class BytesStream(httpx.SyncByteStream):
    def __init__(self, payload):
        self.payload = payload

    def __iter__(self):
        yield self.payload


def frame(payload, *, end=False):
    encoded = json.dumps(payload).encode("utf-8")
    return bytes([2 if end else 0]) + struct.pack(">I", len(encoded)) + encoded


def make_client(handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = cursor_sdk.CursorClient(
        base_url="https://offline-bridge.invalid",
        auth_token="offline-transport-token",
        http_client=http,
        max_retries=0,
        allow_api_key_env_fallback=True,
    )
    return client, http


def sdk_proxy(client):
    class Launcher:
        @classmethod
        def launch_bridge(cls, **kwargs):
            assert kwargs["max_retries"] == 0
            return client

    return SimpleNamespace(
        __version__="1.0.37",
        CursorClient=Launcher,
        LocalAgentOptions=cursor_sdk.LocalAgentOptions,
        LocalAgentStoreConfig=cursor_sdk.LocalAgentStoreConfig,
        SandboxOptions=cursor_sdk.SandboxOptions,
        ModelSelection=cursor_sdk.ModelSelection,
        ModelParameterValue=cursor_sdk.ModelParameterValue,
        AgentOptions=cursor_sdk.AgentOptions,
        SendOptions=cursor_sdk.SendOptions,
        errors=sdk_errors,
    )


def adapter_for(client):
    config = CursorSdkClientConfig(
        sdk_version="1.0.37",
        workspace=workspace,
        state_root=root / "mock-bridge-state",
        store_root=root / "mock-store",
        model="offline-mocktransport",
        parameters=(("fast", "false"),),
        credential_ref="fake-key",
        setting_sources=("project",),
        sandbox="disabled",
    )
    adapter = CursorSdkAdapter(
        config,
        repository_root=workspace,
        sdk_loader=lambda: sdk_proxy(client),
    )
    opened = adapter.open()
    assert opened.value is not None
    return adapter


def control_response(request):
    if request.url.path.endswith("/Ping"):
        return httpx.Response(200, json={"message": "pong"})
    if request.url.path.endswith("/GetVersion"):
        return httpx.Response(
            200,
            json={"bridgeVersion": "1.0.0", "protocolVersion": "sdk.v1"},
        )
    return None


def replay_handler(calls):
    def handler(request):
        calls.append(request.url.path)
        control = control_response(request)
        if control is not None:
            return control
        if request.url.path.endswith("/GetRun"):
            return httpx.Response(
                200,
                json={
                    "run": {
                        "runId": run_id,
                        "agentId": agent_id,
                        "status": "running",
                        "result": "stale running snapshot",
                    }
                },
            )
        if request.url.path.endswith("/ObserveRun"):
            payload = frame(
                {"result": {"result": terminal}, "offset": "7"}
            ) + frame({}, end=True)
            return httpx.Response(200, stream=BytesStream(payload))
        if request.url.path.endswith("/WaitLiveRun"):
            return httpx.Response(200, json={"result": terminal})
        raise AssertionError(request.url.path)

    return handler


# The actual 1.0.37 Run confirms that supports('observe') is false and that
# consuming ObserveRun does not mutate the original GetRun snapshot.
probe_calls = []
probe_client, probe_http = make_client(replay_handler(probe_calls))
probe = probe_client.get_run(
    run_id,
    {"runtime": "local", "agentId": agent_id, "apiKey": secret},
)
assert probe.supports("observe") is False
assert [event.kind for event in probe.observe(after_offset="5")] == ["result"]
assert probe.status == "running"
assert probe.result == "stale running snapshot"
probe_client.close()
probe_http.close()

# The production adapter must obtain the distinct public terminal result after
# ObserveRun instead of converting that stale snapshot.
replay_calls = []
replay_client, replay_http = make_client(replay_handler(replay_calls))
adapter = adapter_for(replay_client)
replay = adapter.stream(agent_id, run_id, mode="replay", after_offset="5")
assert replay.value is not None
assert replay.value.agent_id == agent_id
assert replay.value.run_id == run_id
assert replay.value.status == "finished"
assert replay.value.text == "terminal result from public wait"
assert replay.value.usage == CursorSdkUsage(
    input_tokens=11,
    output_tokens=22,
    cache_read_tokens=33,
    cache_write_tokens=44,
    total_tokens=110,
    reasoning_tokens=6,
)
assert [path.rsplit("/", 1)[-1] for path in replay_calls] == [
    "Ping",
    "GetVersion",
    "GetRun",
    "ObserveRun",
    "WaitLiveRun",
]
adapter.close()
replay_http.close()


def exercise_error(*, status, code, request_id, retry_after, expected):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        control = control_response(request)
        if control is not None:
            return control
        assert request.url.path.endswith("/CreateAgent")
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": "unauthenticated" if status == 401 else "resource_exhausted",
                    "message": f"outer error includes {secret}",
                    "details": [
                        {
                            "sdkErrorCode": code,
                            "message": f"detail includes {secret}",
                            "requestId": request_id,
                            "retryAfter": retry_after,
                        }
                    ],
                }
            },
        )

    client, http = make_client(handler)
    adapter = adapter_for(client)
    outcome = adapter.create_agent()
    assert outcome.failure is not None
    assert outcome.failure.code is expected
    assert secret not in repr(outcome)
    assert sum(path.endswith("/CreateAgent") for path in calls) == 1
    adapter.close()
    http.close()
    return outcome.failure


credential_failure = exercise_error(
    status=401,
    code="SDK_ERROR_CODE_UNAUTHORIZED",
    request_id=secret,
    retry_after=secret,
    expected=CursorSdkErrorCode.AUTHENTICATION_FAILED,
)
assert credential_failure.request_id is None
assert credential_failure.retry_after is None

rate_failure = exercise_error(
    status=429,
    code="SDK_ERROR_CODE_RATE_LIMIT_EXCEEDED",
    request_id="request-safe-429",
    retry_after="17",
    expected=CursorSdkErrorCode.RATE_LIMITED,
)
assert rate_failure.request_id == "request-safe-429"
assert rate_failure.retry_after == "17"


def exercise_cancel(*, returned_agent_id, returned_run_id):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        control = control_response(request)
        if control is not None:
            return control
        if request.url.path.endswith("/GetRun"):
            return httpx.Response(
                200,
                json={
                    "run": {
                        "runId": returned_run_id,
                        "agentId": returned_agent_id,
                        "status": "running",
                    }
                },
            )
        if request.url.path.endswith("/CancelRun"):
            return httpx.Response(200, json={})
        raise AssertionError(request.url.path)

    client, http = make_client(handler)
    adapter = adapter_for(client)
    outcome = adapter.cancel(agent_id, run_id)
    cancel_count = sum(path.endswith("/CancelRun") for path in calls)
    adapter.close()
    http.close()
    return outcome, cancel_count


agent_mismatch, agent_cancel_count = exercise_cancel(
    returned_agent_id="agent-22222222-2222-2222-2222-222222222222",
    returned_run_id=run_id,
)
assert agent_mismatch.failure is not None
assert agent_mismatch.failure.code is CursorSdkErrorCode.INVALID_RESPONSE
assert agent_cancel_count == 0

run_mismatch, run_cancel_count = exercise_cancel(
    returned_agent_id=agent_id,
    returned_run_id="run-22222222-2222-2222-2222-222222222222",
)
assert run_mismatch.failure is not None
assert run_mismatch.failure.code is CursorSdkErrorCode.INVALID_RESPONSE
assert run_cancel_count == 0

exact, exact_cancel_count = exercise_cancel(
    returned_agent_id=agent_id,
    returned_run_id=run_id,
)
assert exact.value is not None
assert exact.value.agent_id == agent_id
assert exact.value.run_id == run_id
assert exact_cancel_count == 1

print(json.dumps({"real_sdk_mocktransport_corrections": "ok"}))
"""
    correction = _run(
        [str(extra_env / "bin/python"), "-c", correction_script],
        cwd=tmp_path,
        env=health_env,
        timeout=60,
    )
    assert json.loads(correction.stdout) == {"real_sdk_mocktransport_corrections": "ok"}

    base_env = tmp_path / "base-venv"
    _run([uv, "venv", "--python", python311, str(base_env)], cwd=tmp_path, env=env)
    _run(
        [uv, "pip", "install", "--python", str(base_env / "bin/python"), str(wheel)],
        cwd=tmp_path,
        env=env,
    )
    base_python = str(base_env / "bin/python")
    _run([str(base_env / "bin/ai_dev_loop"), "--help"], cwd=tmp_path, env=env)
    historical_fixture = (
        repository / "tests/fixtures/phase22_historical/manual_wait_state_pre_phase22_v1.json"
    )
    historical_script = """
import importlib.util
import json
import sys
from pathlib import Path
from ai_dev_loop.scheduler.domain.state import parse_scheduler_state
assert importlib.util.find_spec("cursor_sdk") is None
state = parse_scheduler_state(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
print(state.kind)
"""
    historical = _run(
        [base_python, "-c", historical_script, str(historical_fixture)],
        cwd=tmp_path,
        env=env,
    )
    assert historical.stdout.strip() == "waiting_codex_review_retry"

"""Phase 25.2 scheduler integration with a fake Cursor SDK package/backend."""

from __future__ import annotations

import base64
import itertools
import json
import os
import shutil
import signal
import subprocess
import textwrap
import time
from contextlib import suppress
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest
import yaml
from referencing import Registry, Resource
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.process_output_service import (
    IntegrationProcessOutputReadService,
)
from ai_dev_loop.integration_api.review_service import default_review_read_service
from ai_dev_loop.paths import cursor_conversation_key, cursor_sdk_state_root, schema_path
from ai_dev_loop.scheduler.application.abort import scheduler_abort_run
from ai_dev_loop.scheduler.application.attempt_backend import (
    LaunchRequest,
    ObserveResult,
    TerminationClass,
    UnitLifecycleState,
)
from ai_dev_loop.scheduler.application.attempt_service import AttemptService
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.cursor_recovery_check import (
    scheduler_cursor_recovery_check,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.history import scheduler_history
from ai_dev_loop.scheduler.application.implementer_timeout_retry import (
    scheduler_implementer_retry,
)
from ai_dev_loop.scheduler.application.sequence_materializer import build_sequence_run_context
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    prepare_sequence,
)
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.status import scheduler_list, scheduler_status
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.cursor_contract import (
    cursor_attempt_stderr_rel,
    cursor_sdk_attempt_events_rel,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_ATTEMPTS = itertools.count()


class OwnedProcessGroupBackend:
    """Test backend with real worker/bridge/tool process-group ownership."""

    def __init__(self) -> None:
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.requests: dict[str, LaunchRequest] = {}
        self.terminate_calls: list[str] = []

    def launch(self, request: LaunchRequest) -> None:
        child_env = os.environ.copy()
        child_env.pop("AI_DEV_LOOP_REVIEW_PROMPT_WITNESS_ROOT", None)
        self.requests[request.attempt_id] = request
        self.processes[request.attempt_id] = subprocess.Popen(
            request.agent_argv,
            cwd=request.working_directory,
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def observe(self, *, unit_identity: str, attempt_id: str) -> ObserveResult:
        del unit_identity
        process = self.processes.get(attempt_id)
        if process is None:
            return ObserveResult(
                lifecycle_state=UnitLifecycleState.MISSING,
                owned=True,
                absence_proven=True,
            )
        returncode = process.poll()
        if returncode is None:
            return ObserveResult(lifecycle_state=UnitLifecycleState.ACTIVE, owned=True)
        termination = (
            TerminationClass.SUCCESS
            if returncode == 0
            else (TerminationClass.TIMEOUT if returncode == 124 else TerminationClass.NONZERO_EXIT)
        )
        return ObserveResult(
            lifecycle_state=(
                UnitLifecycleState.INACTIVE if returncode == 0 else UnitLifecycleState.FAILED
            ),
            owned=True,
            exit_code=returncode,
            termination_class=termination,
            result_envelope_path=self.requests[attempt_id].result_envelope_path,
        )

    def terminate(self, *, unit_identity: str, attempt_id: str) -> None:
        del unit_identity
        process = self.processes.get(attempt_id)
        if process is None:
            return
        self.terminate_calls.append(attempt_id)
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


def _wait_for_path(path: Path, *, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for synchronized marker {path.name}")


def _validate_schema(payload: object, name: str) -> None:
    schema = json.loads(schema_path(name).read_text(encoding="utf-8"))
    resources = []
    for path in schema_path(name).parent.glob("*.json"):
        candidate = json.loads(path.read_text(encoding="utf-8"))
        if "$id" in candidate:
            resources.append((candidate["$id"], Resource.from_contents(candidate)))
    jsonschema.Draft202012Validator(
        schema,
        registry=Registry().with_resources(resources),
    ).validate(payload)


def _configure_v2(repo: Path) -> None:
    payload = {
        "version": 2,
        "project": {"name": "fixture-project"},
        "cursor": {
            "runtime": "sdk-local",
            "sdk_version": "1.0.37",
            "model": "grok-4.7",
            "parameters": {
                "context": "256k",
                "reasoning_effort": "high",
                "fast": False,
            },
            "credential_ref": "cursor-test",
            "setting_sources": ["project"],
            "sandbox": "enabled",
        },
        "codex": {
            "command": "codex",
            "review_model": None,
            "review_reasoning_effort": None,
            "review_skill": "review-staged-ai-dev-loop-execution",
            "sandbox": "workspace-write",
        },
        "workflow": {
            "max_review_iterations": 3,
            "require_clean_worktree": True,
            "stage_mode": "all",
            "cursor_timeout_minutes": 2,
            "codex_timeout_minutes": 2,
        },
        "prompt": {
            "directory": "docs/plans",
            "filename_template": "prompt_{plan_stem}.txt",
        },
    }
    (repo / "ai_dev_loop.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    subprocess.run(["git", "add", "ai_dev_loop.yaml"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "sdk config"], cwd=repo, check=True)


def _install_fake_sdk(root: Path) -> None:
    module = root / "cursor_sdk.py"
    module.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import signal
            import sqlite3
            import subprocess
            import sys
            import time
            from pathlib import Path
            from types import SimpleNamespace
            __version__ = "1.0.37"

            class RateLimitError(Exception):
                def __init__(self):
                    super().__init__("synthetic rate limit")
                    self.code = "rate_limited"
                    self.retry_after = "1"

            class AgentBusyError(Exception):
                def __init__(self, message="already has active run"):
                    super().__init__(message)
                    self.message = message
                    self.code = "agent_busy"

            class InternalServerError(Exception):
                def __init__(self, message="bridge failure", code="internal"):
                    super().__init__(message)
                    self.message = message
                    self.code = code

            class NetworkError(Exception):
                def __init__(self, message="synthetic network loss"):
                    super().__init__(message)
                    self.message = message

            class AgentNotFoundError(Exception):
                pass

            errors = SimpleNamespace(
                RateLimitError=RateLimitError,
                AgentBusyError=AgentBusyError,
                InternalServerError=InternalServerError,
                NetworkError=NetworkError,
                AgentNotFoundError=AgentNotFoundError,
            )
            CREATION_RUN_ID = "run-019abc00-1111-4222-8333-777777777777"
            PROMPT_RUN_ID = "run-019abc00-1111-4222-8333-555555555555"
            AGENT_ID = "019abc00-1111-4222-8333-444444444444"

            def _store_path():
                value = os.environ.get("FAKE_CURSOR_SDK_DURABLE_STORE", "")
                return Path(value) if value else None

            def _read_store():
                path = _store_path()
                if path is None or not path.is_file():
                    return {"runs": [], "messages": [], "sends": 0, "conversation_calls": 0, "cancels": []}
                return json.loads(path.read_text(encoding="utf-8"))

            def _write_store(payload):
                path = _store_path()
                if path is None:
                    return
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(path.suffix + ".tmp")
                temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
                temporary.replace(path)

            def _public_creation_status(wire):
                if wire == "creating":
                    return "running"
                return wire

            def _latest_journal_kinds():
                root = os.environ.get("FAKE_CURSOR_SDK_RUN_ROOT", "")
                if not root:
                    return []
                paths = list((Path(root) / "cursor/sdk-attempts").glob("*/dispatch-journal.jsonl"))
                journals = [
                    (path, [json.loads(line)["kind"] for line in path.read_text().splitlines()])
                    for path in paths
                ]
                active = [item for item in journals if "terminal" not in item[1]]
                latest = max(active or journals, key=lambda item: item[0].stat().st_mtime_ns)
                return latest[1]

            def _turn_identity_is_persisted():
                identity = os.environ.get("FAKE_CURSOR_SDK_IDENTITY_PATH", "")
                return bool(identity) and Path(identity).is_file()

            def _spawn_owned_tree():
                marker_root = Path(os.environ["FAKE_CURSOR_SDK_TREE_MARKERS"])
                marker_root.mkdir(parents=True, exist_ok=True)
                bridge_code = '''
            import os, signal, subprocess, sys, time
            from pathlib import Path
            root = Path(os.environ["FAKE_CURSOR_SDK_TREE_MARKERS"])
            (root / "bridge.pid").write_text(str(os.getpid()))
            tool_code = \"\"\"
            import os, signal, time
            from pathlib import Path
            root = Path(os.environ['FAKE_CURSOR_SDK_TREE_MARKERS'])
            (root / 'tool.pid').write_text(str(os.getpid()))
            def stop(signum, frame):
                (root / 'tool.stopped').write_text(str(signum))
                raise SystemExit(0)
            signal.signal(signal.SIGTERM, stop)
            while True: time.sleep(0.1)
            \"\"\"
            tool = subprocess.Popen([sys.executable, "-c", tool_code])
            def stop(signum, frame):
                (root / "bridge.stopped").write_text(str(signum))
                try: tool.wait(timeout=2)
                except subprocess.TimeoutExpired: tool.kill(); tool.wait()
                raise SystemExit(0)
            signal.signal(signal.SIGTERM, stop)
            (root / "tree.ready").write_text("ready")
            while True: time.sleep(0.1)
            '''
                subprocess.Popen(
                    [sys.executable, "-c", bridge_code],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            def _block_reconciliation():
                if os.environ.get("FAKE_CURSOR_SDK_BLOCK_RECONCILIATION") != "1":
                    return
                root = Path(os.environ["FAKE_CURSOR_SDK_TREE_MARKERS"])
                _spawn_owned_tree()
                root.mkdir(parents=True, exist_ok=True)
                (root / "reconciliation.ready").write_text("ready")
                while True: time.sleep(0.1)

            class Options:
                def __init__(self, **kwargs): self.__dict__.update(kwargs)
            LocalAgentOptions = LocalAgentStoreConfig = SandboxOptions = Options
            ModelSelection = ModelParameterValue = AgentOptions = SendOptions = Options

            class Models:
                def list(self, api_key=None):
                    params = [SimpleNamespace(id="context", value="256k"), SimpleNamespace(id="fast", value="false"), SimpleNamespace(id="reasoning_effort", value="high")]
                    return [SimpleNamespace(id="grok-4.7", variants=[SimpleNamespace(params=params)])]

            class Run:
                def __init__(self, workspace, run_id=PROMPT_RUN_ID, agent_id=AGENT_ID, status=None):
                    self.workspace = Path(workspace)
                    self.id = run_id
                    self.agent_id = agent_id
                    self._status = status
                @property
                def status(self):
                    if self._status:
                        return self._status
                    failure_marker = os.environ.get("FAKE_CURSOR_SDK_STREAM_FAILURE_MARKER", "")
                    stream_still_failing = (
                        os.environ.get("FAKE_CURSOR_SDK_STREAM_FAILURE") == "1"
                        and not (failure_marker and Path(failure_marker).is_file())
                    )
                    if (
                        stream_still_failing
                        or os.environ.get("FAKE_CURSOR_SDK_BLOCK_TREE") == "1"
                    ): return ""
                    return os.environ.get("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
                @property
                def result(self): return os.environ.get("FAKE_CURSOR_SDK_TERMINAL_TEXT", "exact fake terminal")
                def events(self):
                    if os.environ.get("FAKE_CURSOR_SDK_RUN_ROOT"):
                        kinds = _latest_journal_kinds()
                        assert kinds[0] == "run_inventory_baseline"
                        assert "send_intent" in kinds
                        assert kinds[-1] == "native_run_bound"
                        assert kinds.count("send_intent") <= 2
                    target = self.workspace / "tracked.txt"
                    prior = target.read_text(encoding="utf-8") if target.is_file() else ""
                    target.write_text(prior + "sdk implementation\\n", encoding="utf-8")
                    yield SimpleNamespace(offset="1", kind="sdk_message", sdk_message=SimpleNamespace(type="status", status="RUNNING"))
                    stream_usage_sequence = os.environ.get("FAKE_CURSOR_SDK_STREAM_USAGE_SEQUENCE_JSON", "").strip()
                    stream_usage = os.environ.get("FAKE_CURSOR_SDK_STREAM_USAGE_JSON", "").strip()
                    usages = json.loads(stream_usage_sequence) if stream_usage_sequence else ([json.loads(stream_usage)] if stream_usage else [])
                    for index, usage in enumerate(usages, start=2):
                        yield SimpleNamespace(offset=str(index), kind="sdk_message", sdk_message=SimpleNamespace(type="usage", usage=SimpleNamespace(**usage)))
                    if os.environ.get("FAKE_CURSOR_SDK_WAIT_FOR_DEADLINE") == "1":
                        marker = Path(os.environ["FAKE_CURSOR_SDK_DEADLINE_MARKER"])
                        marker.with_name(marker.name + ".armed").write_text("armed")
                        while True: time.sleep(0.1)
                    if os.environ.get("FAKE_CURSOR_SDK_TRIGGER_DEADLINE") == "1":
                        signal.raise_signal(signal.SIGALRM)
                    if os.environ.get("FAKE_CURSOR_SDK_RATE_LIMIT") == "1":
                        raise RateLimitError()
                    if os.environ.get("FAKE_CURSOR_SDK_STREAM_FAILURE") == "1":
                        marker = os.environ.get("FAKE_CURSOR_SDK_STREAM_FAILURE_MARKER", "")
                        if marker:
                            Path(marker).write_text("failed")
                        raise RuntimeError("synthetic stream transport failure")
                    if os.environ.get("FAKE_CURSOR_SDK_BLOCK_TREE") == "1":
                        _spawn_owned_tree()
                        while True: time.sleep(0.1)
                    if os.environ.get("FAKE_CURSOR_SDK_NO_PROGRESS") == "1":
                        yield SimpleNamespace(
                            offset="9",
                            kind="sdk_message",
                            sdk_message=SimpleNamespace(
                                type=os.environ.get("FAKE_CURSOR_SDK_STATUS_TYPE", "status"),
                                status=os.environ.get("FAKE_CURSOR_SDK_STATUS_NAME", "ERROR"),
                                message=os.environ.get(
                                    "FAKE_CURSOR_SDK_STATUS_MESSAGE",
                                    "Agent turn stopped after repeated resume attempts made no progress",
                                ),
                                agent_id=os.environ.get("FAKE_CURSOR_SDK_STATUS_AGENT_ID", self.agent_id),
                                run_id=os.environ.get("FAKE_CURSOR_SDK_STATUS_RUN_ID", self.id),
                            ),
                        )
                    assistant_text = os.environ.get("FAKE_CURSOR_SDK_ASSISTANT_TEXT", "")
                    if assistant_text:
                        yield SimpleNamespace(
                            offset="10",
                            kind="sdk_message",
                            sdk_message=SimpleNamespace(type="assistant", message=assistant_text),
                        )
                def observe(self, after_offset=None):
                    if os.environ.get("FAKE_CURSOR_SDK_REPLAY_SUCCESS") != "1":
                        raise RuntimeError("synthetic replay transport failure")
                    if os.environ.get("FAKE_CURSOR_SDK_DISCOVER_UNIQUE_RUN") == "1":
                        target = self.workspace / "tracked.txt"
                        prior = target.read_text(encoding="utf-8") if target.is_file() else ""
                        target.write_text(prior + "sdk implementation\\n", encoding="utf-8")
                    replay_usage_sequence = os.environ.get("FAKE_CURSOR_SDK_REPLAY_USAGE_SEQUENCE_JSON")
                    if replay_usage_sequence is not None:
                        usages = json.loads(replay_usage_sequence)
                    else:
                        stream_usage_sequence = os.environ.get("FAKE_CURSOR_SDK_STREAM_USAGE_SEQUENCE_JSON", "").strip()
                        stream_usage = os.environ.get("FAKE_CURSOR_SDK_STREAM_USAGE_JSON", "").strip()
                        usages = json.loads(stream_usage_sequence) if stream_usage_sequence else ([json.loads(stream_usage)] if stream_usage else [])
                    for index, usage in enumerate(usages, start=2):
                        yield SimpleNamespace(offset=str(index), kind="sdk_message", sdk_message=SimpleNamespace(type="usage", usage=SimpleNamespace(**usage)))
                    yield SimpleNamespace(offset=str(len(usages) + 2), kind="sdk_message", sdk_message=SimpleNamespace(type="status", status="FINISHED"))
                def wait(self):
                    usage_json = os.environ.get("FAKE_CURSOR_SDK_USAGE_JSON", "").strip()
                    return SimpleNamespace(
                        status=self._status or os.environ.get("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished"),
                        result="" if self._status in {"queued", "running", "cancelled", "canceled"} else os.environ.get("FAKE_CURSOR_SDK_TERMINAL_TEXT", "exact fake terminal"),
                        usage=(
                            None
                            if not usage_json or self._status in {"queued", "running", "cancelled", "canceled"}
                            else SimpleNamespace(**json.loads(usage_json))
                        ),
                    )
                def cancel(self):
                    store = _read_store()
                    for item in store.get("runs", []):
                        if item["id"] == self.id:
                            item["status"] = "cancelled"
                    store.setdefault("cancels", []).append(self.id)
                    _write_store(store)
                    self._status = "cancelled"
                def conversation(self):
                    store = _read_store()
                    store["conversation_calls"] = int(store.get("conversation_calls", 0)) + 1
                    _write_store(store)
                    raise TimeoutError("GetRunConversation timed out")

            class Agent:
                agent_id = "019abc00-1111-4222-8333-444444444444"
                def __init__(self, workspace): self.workspace = workspace
                def list_messages(self, options=None):
                    del options
                    forced = os.environ.get("FAKE_CURSOR_SDK_MESSAGE_COUNT", "")
                    if forced != "":
                        return [
                            SimpleNamespace(type="user", uuid=str(index), agent_id=self.agent_id)
                            for index in range(int(forced))
                        ]
                    store = _read_store()
                    return [
                        SimpleNamespace(type=str(item.get("type", "user")), uuid=str(index), agent_id=self.agent_id)
                        for index, item in enumerate(store.get("messages", []))
                    ]
                def send(self, prompt, options):
                    local = getattr(options, "local", None)
                    assert not (isinstance(local, dict) and local.get("force"))
                    assert getattr(local, "force", None) in {None, False}
                    counter = os.environ.get("FAKE_CURSOR_SDK_SEND_COUNTER", "")
                    if counter:
                        path = Path(counter)
                        value = int(path.read_text()) if path.is_file() else 0
                        path.write_text(str(value + 1))
                    identity = os.environ.get("FAKE_CURSOR_SDK_IDENTITY_PATH", "")
                    if identity:
                        assert Path(identity).is_file()
                        kinds = _latest_journal_kinds()
                        assert kinds[0] == "run_inventory_baseline"
                        assert kinds[-1] == "send_intent"
                        assert "native_run_bound" not in kinds
                    if os.environ.get("FAKE_CURSOR_SDK_SEND_UNCERTAIN") == "1":
                        raise RuntimeError("synthetic send result loss")
                    store = _read_store()
                    store["send_attempts"] = int(store.get("send_attempts", 0)) + 1
                    mode = os.environ.get("FAKE_CURSOR_SDK_FIRST_SEND_MODE", "")
                    if mode == "accept-network" and store["send_attempts"] == 1:
                        store.setdefault("runs", []).append(
                            {
                                "id": os.environ.get(
                                    "FAKE_CURSOR_SDK_ACCEPTED_RUN_ID",
                                    "run-019abc00-1111-4222-8333-888888888888",
                                ),
                                "status": "finished",
                                "agent_id": self.agent_id,
                                "text": "accepted before the error",
                            }
                        )
                        _write_store(store)
                        raise NetworkError("lost before acceptance")
                    if mode == "message-busy" and store["send_attempts"] == 1:
                        _write_store(store)
                        raise InternalServerError("agent already has active run", code="internal")
                    if mode == "structured-busy" and store["send_attempts"] == 1:
                        _write_store(store)
                        raise InternalServerError("agent busy", code="agent_busy")
                    if mode == "typed-busy" and store["send_attempts"] == 1:
                        _write_store(store)
                        raise AgentBusyError("already has active run")
                    if mode == "typed-busy-twice" and store["send_attempts"] <= 2:
                        _write_store(store)
                        raise AgentBusyError("already has active run")
                    active = [
                        item for item in store.get("runs", [])
                        if item.get("status") in {"queued", "running"}
                    ]
                    if active and os.environ.get("FAKE_CURSOR_SDK_CREATION_OCCUPANT"):
                        evidence = os.environ.get("FAKE_CURSOR_SDK_BUSY_EVIDENCE", "typed")
                        if evidence == "structured":
                            raise InternalServerError("agent busy", code="agent_busy")
                        if evidence == "message":
                            raise InternalServerError("agent already has active run", code="internal")
                        if evidence == "network":
                            raise NetworkError("lost before acceptance")
                        raise AgentBusyError("already has active run")
                    if os.environ.get("FAKE_CURSOR_SDK_ACCEPT_NEW_RUN_ON_BUSY") == "1" and active:
                        accepted = {
                            "id": os.environ.get("FAKE_CURSOR_SDK_ACCEPTED_RUN_ID", "run-019abc00-1111-4222-8333-888888888888"),
                            "status": "finished",
                            "agent_id": self.agent_id,
                            "text": "accepted despite busy",
                        }
                        store.setdefault("runs", []).append(accepted)
                        _write_store(store)
                        raise AgentBusyError("already has active run")
                    store["sends"] = int(store.get("sends", 0)) + 1
                    store.setdefault("prompts", []).append(prompt)
                    store.setdefault("messages", []).append({"type": "user"})
                    prompt_ids = [
                        item.get("id")
                        for item in store.get("runs", [])
                        if item.get("id") == PROMPT_RUN_ID
                    ]
                    run_id = (
                        PROMPT_RUN_ID
                        if not prompt_ids
                        else "run-019abc00-1111-4222-8333-555555555556"
                    )
                    store.setdefault("runs", []).append(
                        {"id": run_id, "status": "finished", "agent_id": self.agent_id}
                    )
                    _write_store(store)
                    return Run(self.workspace, run_id=run_id)

            class Client:
                def __init__(self, workspace): self.workspace = workspace; self.models = Models()
                def ping(self): return "pong"
                def get_version(self): return {"bridgeVersion": "fake", "protocolVersion": "v1"}
                def me(self, api_key=None):
                    if (
                        os.environ.get("FAKE_CURSOR_SDK_TURN_PREFLIGHT_FAILURE") == "1"
                        and _turn_identity_is_persisted()
                    ):
                        raise RuntimeError("synthetic preflight failure")
                    return SimpleNamespace(id="fake-user")
                def create_agent(self, options):
                    counter = os.environ.get("FAKE_CURSOR_SDK_CREATE_COUNTER", "")
                    if counter:
                        path = Path(counter)
                        value = int(path.read_text()) if path.is_file() else 0
                        path.write_text(str(value + 1))
                    if os.environ.get("FAKE_CURSOR_SDK_RUN_ROOT"):
                        assert _latest_journal_kinds() == ["create_intent"]
                    if os.environ.get("FAKE_CURSOR_SDK_CREATE_UNCERTAIN") == "1":
                        raise RuntimeError("synthetic create result loss")
                    wire = os.environ.get("FAKE_CURSOR_SDK_CREATION_OCCUPANT", "")
                    if wire:
                        store = _read_store()
                        runs = [{
                            "id": CREATION_RUN_ID,
                            "status": _public_creation_status(wire),
                            "wire": wire,
                            "agent_id": AGENT_ID,
                        }]
                        extra = os.environ.get("FAKE_CURSOR_SDK_EXTRA_CREATION_RUN", "")
                        if extra:
                            runs.append({"id": extra, "status": "running", "agent_id": AGENT_ID})
                        store["runs"] = runs
                        store["messages"] = []
                        store["sends"] = 0
                        store["cancels"] = []
                        store["conversation_calls"] = 0
                        _write_store(store)
                    return Agent(options.local.cwd)
                def resume_agent(self, agent_id, options):
                    if os.environ.get("FAKE_CURSOR_SDK_MISSING_BOUND_AGENT") == "1":
                        raise AgentNotFoundError("synthetic bound agent missing")
                    if os.environ.get("FAKE_CURSOR_SDK_INJECT_FOREIGN_ON_RESUME") == "1":
                        store = _read_store()
                        foreign_id = "run-019abc00-1111-4222-8333-999999999999"
                        if not any(item.get("id") == foreign_id for item in store.get("runs", [])):
                            store.setdefault("runs", []).append(
                                {"id": foreign_id, "status": "running", "agent_id": agent_id}
                            )
                            _write_store(store)
                    return Agent(options.local.cwd)
                def list_runs(self, agent_id, **kwargs):
                    class Page(list):
                        def __init__(self, items, next_cursor=None):
                            super().__init__(items)
                            self.next_cursor = next_cursor
                    cursor = kwargs.get("cursor")
                    if cursor == "stuck":
                        return Page([], "stuck")
                    send_counter = os.environ.get("FAKE_CURSOR_SDK_SEND_COUNTER", "")
                    send_completed = (
                        bool(send_counter)
                        and Path(send_counter).is_file()
                        and Path(send_counter).read_text() == "1"
                    )
                    runs = []
                    if _store_path() is not None:
                        for item in _read_store().get("runs", []):
                            if item.get("agent_id") not in {None, agent_id}:
                                continue
                            runs.append(Run(self.workspace, item["id"], agent_id, item.get("status")))
                    if (
                        os.environ.get("FAKE_CURSOR_SDK_DISCOVER_UNIQUE_RUN") == "1"
                        and send_completed
                        and not any(run.id == PROMPT_RUN_ID for run in runs)
                    ):
                        runs.append(Run(self.workspace))
                    if os.environ.get("FAKE_CURSOR_SDK_STUCK_CURSOR") == "1" and cursor is None and runs:
                        return Page(runs, "stuck")
                    return Page(runs)
                def get_run(self, run_id, options):
                    _block_reconciliation()
                    if os.environ.get("FAKE_CURSOR_SDK_NATIVE_UNAVAILABLE") == "1":
                        raise RuntimeError("synthetic native observation unavailable")
                    for item in _read_store().get("runs", []):
                        if item.get("id") == run_id:
                            return Run(self.workspace, run_id, item.get("agent_id", AGENT_ID), item.get("status"))
                    return Run(self.workspace, run_id)
                def close(self):
                    marker_value = os.environ.get("FAKE_CURSOR_SDK_DEADLINE_MARKER", "")
                    if not marker_value:
                        return
                    marker = Path(marker_value)
                    armed = marker.with_name(marker.name + ".armed")
                    if not armed.is_file():
                        return
                    armed.unlink()
                    marker.with_name(marker.name + ".closing").write_text("closing")
                    try:
                        time.sleep(2)
                    except BaseException:
                        marker.with_name(marker.name + ".repeated").write_text("repeated")
                        raise

            class CursorClient:
                @staticmethod
                def launch_bridge(workspace, **kwargs):
                    if (
                        os.environ.get("FAKE_CURSOR_SDK_TURN_OPEN_FAILURE") == "1"
                        and _turn_identity_is_persisted()
                    ):
                        raise RuntimeError("synthetic open failure")
                    local = kwargs.get("local")
                    if getattr(getattr(local, "store", None), "type", None) == "sqlite":
                        command = kwargs.get("command")
                        assert isinstance(command, tuple)
                        assert len(command) == 4
                        assert Path(command[0]).name == "node"
                        assert command[1] == "--import"
                        assert Path(command[2]).name == "native_store_initializer.mjs"
                        assert Path(command[3]).name == "cursor-sdk-bridge.js"
                        state_root = Path(kwargs["state_root"])
                        state_root.mkdir(parents=True, exist_ok=True)
                        with sqlite3.connect(state_root / "index.db") as connection:
                            connection.execute(
                                "CREATE TABLE IF NOT EXISTS fake_native_state "
                                "(key TEXT PRIMARY KEY)"
                            )
                    return Client(workspace)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    dist = root / "cursor_sdk-1.0.37.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: cursor-sdk\nVersion: 1.0.37\n", encoding="utf-8"
    )
    bridge_root = root / "cursor_sdk/_vendor/bridge"
    node = bridge_root / "bin/node"
    entrypoint = bridge_root / "dist/bin/cursor-sdk-bridge.js"
    node.parent.mkdir(parents=True)
    entrypoint.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    node.chmod(0o755)
    entrypoint.write_text("// synthetic bridge entrypoint\n", encoding="utf-8")


def _credential(isolated_xdg: Path) -> None:
    credentials = isolated_xdg / "config/ai_dev_loop/credentials"
    credentials.mkdir(parents=True, mode=0o700)
    credentials.parent.chmod(0o700)
    credentials.chmod(0o700)
    key = credentials / "cursor-test"
    key.write_text("fake-test-credential", encoding="utf-8")
    key.chmod(0o600)


def test_sdk_initial_and_correction_reach_acceptance_with_durable_exact_identity(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    del fake_clis
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", "019abc00-1111-4222-8333-666666666666")
    monkeypatch.setenv(
        "FAKE_CURSOR_SDK_TERMINAL_TEXT",
        "ordinary prefix fake-test-credential ordinary suffix",
    )
    _configure_v2(git_repo)
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "tracked baseline"], cwd=git_repo, check=True)
    fake_sdk = tmp_path / "fake-sdk"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    prior = __import__("os").environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk) + (":" + prior if prior else ""))

    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(
            SubmitOptions(
                short_name="SDK normal flow",
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_SESSION,
                codex_review_model="gpt-6.1-sol",
                codex_review_reasoning_effort="high",
                db_path=db,
                artifact_root=artifacts_root,
            )
        ).run_id
    run_root = ProtectedArtifactStore(artifacts_root).run_root(run_id)
    monkeypatch.setenv("FAKE_CURSOR_SDK_RUN_ROOT", str(run_root))
    monkeypatch.setenv(
        "FAKE_CURSOR_SDK_IDENTITY_PATH",
        str(run_root / "cursor/sdk-identity.json"),
    )
    start_run(run_id, db_path=db)
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 17, 12, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase252-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(60):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
        if state.kind == "completed":
            break
        assert state.kind != "blocked", getattr(state, "block_reason_summary", "")
    assert state.kind == "completed"
    assert state.context.schema_version == 9
    _validate_schema(
        state.context.model_dump(mode="json"),
        "scheduler-submitted-run-context-v9.json",
    )
    assert state.cursor.chat_id is None
    assert state.cursor.sdk is not None
    assert state.cursor.sdk.schema_version == 2
    assert state.cursor.sdk.store_backend == "sqlite"
    assert state.cursor.sdk.native_store_layout_version == 1
    assert state.cursor.sdk.agent_id == "019abc00-1111-4222-8333-444444444444"
    assert state.cursor.sdk.native_run_id == "run-019abc00-1111-4222-8333-555555555555"
    assert state.cursor.iteration == 2
    assert state.codex.reviewer_session_id == "019abc00-1111-4222-8333-666666666666"
    assert (run_root / state.cursor.sdk.identity_artifact_path).is_file()
    identity = json.loads(
        (run_root / state.cursor.sdk.identity_artifact_path).read_text(encoding="utf-8")
    )
    assert identity["schema_version"] == 2
    assert identity["store_backend"] == "sqlite"
    assert identity["native_store_layout_version"] == 1
    final_responses = list((run_root / "cursor/iterations").glob("*/*/final.txt"))
    assert final_responses
    for final_response in final_responses:
        text = final_response.read_text(encoding="utf-8")
        assert "fake-test-credential" not in text
        assert "ordinary prefix [redacted] ordinary suffix" in text
    review_prompts = list((run_root / "codex/reviews").glob("*.prompt.txt"))
    assert len(review_prompts) == 2
    for review_prompt in review_prompts:
        text = review_prompt.read_text(encoding="utf-8")
        assert "fake-test-credential" not in text
        assert "ordinary prefix [redacted] ordinary suffix" in text
    with store.begin_read() as conn:
        sdk_turns = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events WHERE run_id=? AND event_kind='cursor_sdk_turn_completed'",
            (run_id,),
        ).fetchone()[0]
        sdk_attempt_id = str(
            conn.execute(
                """
                SELECT a.attempt_id
                FROM scheduler_attempts a
                JOIN scheduler_effects e ON e.dispatch_id = a.dispatch_id
                WHERE a.run_id=? AND e.effect_kind='cursor.run_turn'
                ORDER BY a.created_at LIMIT 1
                """,
                (run_id,),
            ).fetchone()[0]
        )
        attempts_before_replay = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    assert sdk_turns == 2
    for _ in range(3):
        tick.run_once()
    with store.begin_read() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == attempts_before_replay
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_events "
                "WHERE run_id=? AND event_kind='cursor_sdk_turn_completed'",
                (run_id,),
            ).fetchone()[0]
            == sdk_turns
        )
    output = IntegrationProcessOutputReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    )
    stdout = output.read_process_output(run_id, attempt_id=sdk_attempt_id, stream="stdout")
    stderr = output.read_process_output(run_id, attempt_id=sdk_attempt_id, stream="stderr")
    assert stdout.available is True and stdout.stream_format == "jsonl"
    assert "sdk_message" in base64.b64decode(stdout.content_base64).decode("utf-8")
    assert stderr.available is True and stderr.returned_bytes == 0
    assert state.cursor.sdk is not None
    events = ProtectedArtifactStore(artifacts_root).run_root(
        run_id
    ) / cursor_sdk_attempt_events_rel(1, sdk_attempt_id)
    events.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(IntegrationApiError, match="failed verification"):
        output.read_process_output(run_id, attempt_id=sdk_attempt_id, stream="stdout")


@pytest.mark.parametrize("damage", ["missing", "empty", "truncated", "corrupt", "missing_agent"])
def test_bound_sdk_agent_with_unavailable_sqlite_state_blocks_before_send(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    damage: str,
) -> None:
    del fake_clis
    _configure_v2(git_repo)
    fake_sdk = tmp_path / "fake-sdk-missing-native"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk))
    send_counter = tmp_path / "send-count"
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(
            SubmitOptions(
                short_name="Missing SQLite state",
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_SESSION,
                codex_review_model="gpt-6.1-sol",
                codex_review_reasoning_effort="high",
                db_path=db,
                artifact_root=artifacts_root,
            )
        ).run_id
    start_run(run_id, db_path=db)
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 17, 12, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase257-missing-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(20):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
        if state.kind == "cursor_ready":
            break
    assert state.kind == "cursor_ready"
    assert state.cursor.sdk is not None
    assert state.cursor.sdk.store_backend == "sqlite"
    owner_root = cursor_sdk_state_root() / cursor_conversation_key(run_id)
    assert (owner_root / "index.db").is_file()
    if damage == "missing_agent":
        monkeypatch.setenv("FAKE_CURSOR_SDK_MISSING_BOUND_AGENT", "1")
    elif damage == "missing":
        shutil.rmtree(owner_root)
    elif damage == "empty":
        (owner_root / "index.db").write_bytes(b"")
    elif damage == "truncated":
        (owner_root / "index.db").write_bytes(b"SQLite format 3\x00truncated")
    else:
        (owner_root / "index.db").write_bytes(b"not a sqlite database")

    for _ in range(10):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            reservation = store.get_reservation_for_run(conn, run_id)
        if state.kind == "blocked":
            break
    assert state.kind == "blocked"
    assert state.block_reason_kind == "cursor_sdk_native_unavailable"
    assert reservation is not None and reservation["status"] == "active"
    assert owner_root.exists() is (damage != "missing")
    assert not send_counter.exists()


def test_historical_reads_need_no_sdk_native_state(
    isolated_xdg: Path,
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures/phase21_4_pre_21_4_historical"
    manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))
    run_id = str(manifest["run_id"])
    attempt_id = str(manifest["attempt_id"])
    state_root = tmp_path / "historical-state"
    state_root.mkdir()
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    shutil.copy2(fixture / "engine.sqlite3", db)
    shutil.copytree(fixture / "artifacts", artifacts_root)
    before = db.read_bytes()
    assert not cursor_sdk_state_root().exists()

    listed = scheduler_list(db_path=db)
    assert [item.run_id for item in listed] == [run_id]
    assert scheduler_status(run_id, db_path=db).summary.run_id == run_id
    assert scheduler_history(run_id, db_path=db).entries
    review = default_review_read_service(db_path=db, artifact_root=artifacts_root)
    detail = review.inspect_review(run_id, attempt_id=attempt_id)
    assert detail.result_state == "valid"
    content = review.read_review_content(
        run_id,
        attempt_id=attempt_id,
        content_kind="review-markdown",
    )
    assert content.available is True and content.returned_bytes > 0
    assert not cursor_sdk_state_root().exists()
    assert db.read_bytes() == before


@pytest.mark.parametrize(
    ("failure_env", "expected_reason", "expected_attempts"),
    [
        ("FAKE_CURSOR_SDK_CREATE_UNCERTAIN", "cursor_sdk_create_uncertain", 1),
        ("FAKE_CURSOR_SDK_SEND_UNCERTAIN", "cursor_sdk_ambiguous_missing", 3),
        ("FAKE_CURSOR_SDK_STREAM_FAILURE", "cursor_sdk_reconciliation_failed", 3),
    ],
)
def test_sdk_lost_identity_result_holds_reservation_and_rejects_recovery(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_env: str,
    expected_reason: str,
    expected_attempts: int,
) -> None:
    del fake_clis
    _configure_v2(git_repo)
    fake_sdk = tmp_path / f"fake-sdk-{expected_reason}"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    monkeypatch.setenv(failure_env, "1")
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk))
    create_counter = tmp_path / "create-count"
    send_counter = tmp_path / "send-count"
    monkeypatch.setenv("FAKE_CURSOR_SDK_CREATE_COUNTER", str(create_counter))
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(
            SubmitOptions(
                short_name="SDK uncertainty",
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_SESSION,
                codex_review_model="gpt-6.1-sol",
                codex_review_reasoning_effort="high",
                db_path=db,
                artifact_root=artifacts_root,
            )
        ).run_id
    start_run(run_id, db_path=db)
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 17, 14, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase252-uncertain-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(30):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
        if state.kind == "blocked":
            break
    assert state.kind == "blocked"
    assert state.block_reason_kind == expected_reason
    for _ in range(5):
        tick.run_once()
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        attempts = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        reservation = store.get_reservation_for_run(conn, run_id)
        turn_row = conn.execute(
            """
            SELECT a.attempt_id
            FROM scheduler_attempts a
            JOIN scheduler_effects e ON e.dispatch_id=a.dispatch_id
            WHERE a.run_id=? AND e.effect_kind='cursor.run_turn'
            ORDER BY a.created_at DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    assert state.kind == "blocked" and attempts == expected_attempts
    assert reservation is not None and reservation["status"] == "active"
    assert create_counter.read_text(encoding="utf-8") == "1"
    expected_sends = "0" if failure_env == "FAKE_CURSOR_SDK_CREATE_UNCERTAIN" else "1"
    actual_sends = send_counter.read_text(encoding="utf-8") if send_counter.is_file() else "0"
    assert actual_sends == expected_sends
    check = scheduler_cursor_recovery_check(run_id, db_path=db, artifact_root=artifacts_root)
    assert check.reason_code == "ineligible_block_reason"
    assert check.recovery_supported is False
    with pytest.raises(SchedulerEngineError, match="no retryable Cursor timeout"):
        scheduler_implementer_retry(run_id, db_path=db)
    with pytest.raises(SchedulerEngineError, match="requires a cursor_failure block"):
        scheduler_implementer_retry(run_id, db_path=db, force=True)
    if failure_env in {"FAKE_CURSOR_SDK_SEND_UNCERTAIN", "FAKE_CURSOR_SDK_STREAM_FAILURE"}:
        assert turn_row is not None
        output_reader = IntegrationProcessOutputReadService(
            SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
        )
        stdout = output_reader.read_process_output(
            run_id,
            attempt_id=str(turn_row["attempt_id"]),
            stream="stdout",
        )
        stderr = output_reader.read_process_output(
            run_id,
            attempt_id=str(turn_row["attempt_id"]),
            stream="stderr",
        )
        assert stdout.available is True and stdout.complete is True
        assert stderr.available is True and stderr.complete is True
        assert stderr.returned_bytes == 0 and stderr.stored_bytes == 0
        if failure_env == "FAKE_CURSOR_SDK_STREAM_FAILURE":
            assert "sdk_message" in base64.b64decode(stdout.content_base64).decode("utf-8")
            assert stdout.stored_bytes == stdout.available_bytes
        else:
            assert stdout.returned_bytes == 0 and stdout.stored_bytes == 0


@pytest.mark.parametrize(
    "failure_env",
    ["FAKE_CURSOR_SDK_TURN_OPEN_FAILURE", "FAKE_CURSOR_SDK_TURN_PREFLIGHT_FAILURE"],
)
def test_sdk_early_turn_failure_publishes_authenticated_empty_capture(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_env: str,
) -> None:
    del fake_clis
    _configure_v2(git_repo)
    fake_sdk = tmp_path / f"fake-sdk-{failure_env.lower()}"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    monkeypatch.setenv(failure_env, "1")
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk))
    send_counter = tmp_path / "early-failure-send-count"
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(
            SubmitOptions(
                short_name="SDK early failure capture",
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_SESSION,
                codex_review_model="gpt-6.1-sol",
                codex_review_reasoning_effort="high",
                db_path=db,
                artifact_root=artifacts_root,
            )
        ).run_id
    run_root = ProtectedArtifactStore(artifacts_root).run_root(run_id)
    monkeypatch.setenv("FAKE_CURSOR_SDK_RUN_ROOT", str(run_root))
    monkeypatch.setenv("FAKE_CURSOR_SDK_IDENTITY_PATH", str(run_root / "cursor/sdk-identity.json"))
    start_run(run_id, db_path=db)
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 17, 14, 20, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase252-early-failure-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(30):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
        if state.kind == "blocked":
            break
    assert state.kind == "blocked"
    assert state.block_reason_kind == "cursor_sdk_preflight_failed"
    assert not send_counter.exists()
    with store.begin_read() as conn:
        turn_row = conn.execute(
            """
            SELECT a.attempt_id
            FROM scheduler_attempts a
            JOIN scheduler_effects e ON e.dispatch_id=a.dispatch_id
            WHERE a.run_id=? AND e.effect_kind='cursor.run_turn'
            ORDER BY a.created_at DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    assert turn_row is not None
    attempt_id = str(turn_row["attempt_id"])
    output_reader = IntegrationProcessOutputReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    )
    stdout = output_reader.read_process_output(run_id, attempt_id=attempt_id, stream="stdout")
    stderr = output_reader.read_process_output(run_id, attempt_id=attempt_id, stream="stderr")
    for output in (stdout, stderr):
        assert output.available is True and output.complete is True
        assert output.returned_bytes == 0 and output.stored_bytes == 0

    (run_root / cursor_attempt_stderr_rel(1, attempt_id)).write_text(
        "tampered after terminal binding\n", encoding="utf-8"
    )
    with pytest.raises(IntegrationApiError, match="failed verification"):
        output_reader.read_process_output(run_id, attempt_id=attempt_id, stream="stderr")


def test_sdk_timeout_stops_real_owned_bridge_and_tool_tree_without_redispatch(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    _configure_v2(git_repo)
    fake_sdk = tmp_path / "fake-sdk-timeout-tree"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    markers = tmp_path / "timeout-tree-markers"
    send_counter = tmp_path / "timeout-send-count"
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk))
    monkeypatch.setenv("FAKE_CURSOR_SDK_BLOCK_TREE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TREE_MARKERS", str(markers))
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(
            SubmitOptions(
                short_name="SDK owned timeout",
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_SESSION,
                codex_review_model="gpt-6.1-sol",
                codex_review_reasoning_effort="high",
                db_path=db,
                artifact_root=artifacts_root,
            )
        ).run_id
    run_root = ProtectedArtifactStore(artifacts_root).run_root(run_id)
    monkeypatch.setenv("FAKE_CURSOR_SDK_RUN_ROOT", str(run_root))
    monkeypatch.setenv("FAKE_CURSOR_SDK_IDENTITY_PATH", str(run_root / "cursor/sdk-identity.json"))
    original_binding = AttemptService._cursor_binding

    def one_second_sdk_turn(self, state, *, effect_kind, run_id):  # type: ignore[no-untyped-def]
        binding = original_binding(self, state, effect_kind=effect_kind, run_id=run_id)
        if binding.get("runtime") == "sdk-local" and effect_kind == "cursor.run_turn":
            binding["timeout_seconds"] = 1
        return binding

    monkeypatch.setattr(AttemptService, "_cursor_binding", one_second_sdk_turn)
    start_run(run_id, db_path=db)
    store = SqliteSchedulerStore(db)
    backend = OwnedProcessGroupBackend()
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 17, 14, 45, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase252-real-timeout-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=backend,
        preflight_port=OkPreflightPort(),
    )
    state = None
    for _ in range(180):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
        if state.kind == "blocked":
            break
        time.sleep(0.02)
    assert state is not None and state.kind == "blocked"
    assert state.block_reason_kind == "cursor_sdk_reconciliation_failed"
    _wait_for_path(markers / "tree.ready")
    _wait_for_path(markers / "bridge.stopped")
    _wait_for_path(markers / "tool.stopped")
    assert send_counter.read_text() == "1"
    with store.begin_read() as conn:
        attempts_before = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        reservation = store.get_reservation_for_run(conn, run_id)
    for _ in range(4):
        tick.run_once()
    with store.begin_read() as conn:
        attempts_after = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    assert attempts_after == attempts_before == 3
    assert reservation is not None and reservation["status"] == "active"


def test_sdk_abort_stops_real_owned_bridge_and_tool_tree_and_suppresses_late_adoption(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    _configure_v2(git_repo)
    fake_sdk = tmp_path / "fake-sdk-abort-tree"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    markers = tmp_path / "abort-tree-markers"
    send_counter = tmp_path / "abort-send-count"
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk))
    monkeypatch.setenv("FAKE_CURSOR_SDK_BLOCK_TREE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TREE_MARKERS", str(markers))
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(send_counter))
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(
            SubmitOptions(
                short_name="SDK owned abort",
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_SESSION,
                codex_review_model="gpt-6.1-sol",
                codex_review_reasoning_effort="high",
                db_path=db,
                artifact_root=artifacts_root,
            )
        ).run_id
    run_root = ProtectedArtifactStore(artifacts_root).run_root(run_id)
    monkeypatch.setenv("FAKE_CURSOR_SDK_RUN_ROOT", str(run_root))
    monkeypatch.setenv("FAKE_CURSOR_SDK_IDENTITY_PATH", str(run_root / "cursor/sdk-identity.json"))
    start_run(run_id, db_path=db)
    store = SqliteSchedulerStore(db)
    backend = OwnedProcessGroupBackend()
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 17, 14, 50, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase252-real-abort-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=backend,
        preflight_port=OkPreflightPort(),
    )
    turn_attempt_id = ""
    for _ in range(180):
        tick.run_once()
        with store.begin_read() as conn:
            active = store.get_nonterminal_attempt_for_run(conn, run_id)
            if (
                active is not None
                and active["iteration"] is not None
                and int(active["iteration"]) == 1
            ):
                turn_attempt_id = str(active["attempt_id"])
        if markers.joinpath("tree.ready").is_file():
            break
        time.sleep(0.02)
    _wait_for_path(markers / "tree.ready")
    assert turn_attempt_id and send_counter.read_text() == "1"

    aborted = scheduler_abort_run(
        run_id,
        db_path=db,
        artifact_root=artifacts_root,
        backend=backend,
    )

    assert aborted.abort_persisted is True
    _wait_for_path(markers / "bridge.stopped")
    _wait_for_path(markers / "tool.stopped")
    for _ in range(4):
        tick.run_once()
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        adopted = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events "
            "WHERE run_id=? AND event_kind='cursor_sdk_turn_completed'",
            (run_id,),
        ).fetchone()[0]
        downstream = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts "
            "WHERE run_id=? AND component IN ('staging','codex')",
            (run_id,),
        ).fetchone()[0]
    assert state.kind == "aborted" and adopted == 0 and downstream == 0
    unavailable = IntegrationProcessOutputReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    ).read_process_output(run_id, attempt_id=turn_attempt_id, stream="stdout")
    assert unavailable.available is False and unavailable.reason == "not_captured"


@pytest.mark.parametrize(
    "termination_scenario",
    [
        {"terminate_unavailable": True},
        {"stays_active_after_terminate": True},
    ],
)
def test_sdk_completed_result_waits_for_proved_process_tree_termination(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    termination_scenario: dict[str, bool],
) -> None:
    del fake_clis
    _configure_v2(git_repo)
    fake_sdk = tmp_path / "fake-sdk-termination"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk))
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(
            SubmitOptions(
                short_name="SDK termination guard",
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_SESSION,
                codex_review_model="gpt-6.1-sol",
                codex_review_reasoning_effort="high",
                db_path=db,
                artifact_root=artifacts_root,
            )
        ).run_id
    start_run(run_id, db_path=db)
    store = SqliteSchedulerStore(db)
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(
            active_ticks=0,
            exit_code=0,
            **termination_scenario,
        )
    )
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 17, 14, 30, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase252-termination-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=backend,
        preflight_port=OkPreflightPort(),
    )
    for _ in range(30):
        tick.run_once()
        with store.begin_read() as conn:
            attempt = store.get_nonterminal_attempt_for_run(conn, run_id)
        if attempt is not None and str(attempt["status"]) == "uncertain":
            break
    assert attempt is not None and str(attempt["status"]) == "uncertain"
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        reservation = store.get_reservation_for_run(conn, run_id)
        downstream = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts "
            "WHERE run_id=? AND component IN ('staging','codex')",
            (run_id,),
        ).fetchone()[0]
    assert state.kind == "preflight_complete"
    assert reservation is not None and reservation["status"] == "active"
    assert downstream == 0
    check = scheduler_cursor_recovery_check(run_id, db_path=db, artifact_root=artifacts_root)
    assert check.reason_code == "ineligible_not_blocked"
    assert check.recovery_supported is False

    aborted = scheduler_abort_run(
        run_id,
        db_path=db,
        artifact_root=artifacts_root,
        backend=backend,
    )
    assert aborted.abort_persisted is True and aborted.termination_pending is True
    with store.begin_read() as conn:
        assert store.has_unresolved_abort_hold(conn, run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None


def test_manifest_v4_freezes_named_mixed_provider_entries(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    _configure_v2(git_repo)
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "mixed baseline"], cwd=git_repo, check=True)
    fake_sdk = tmp_path / "fake-sdk-mixed"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    prior = __import__("os").environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk) + (":" + prior if prior else ""))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", "019abc00-1111-4222-8333-777777777777")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", "019abc00-1111-4222-8333-888888888888")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "codex-final.txt")
    manifest = git_repo / "phase25-v4.yaml"
    phase_cursor = {
        "runtime": "sdk-local",
        "model": "grok-4.7",
        "parameters": {"context": "256k", "reasoning_effort": "high", "fast": False},
    }
    phases = []
    for index, provider in enumerate(("cursor", "codex"), start=1):
        implementer = (
            {"provider": "cursor"}
            if provider == "cursor"
            else {
                "provider": "codex",
                "command": "codex",
                "model": "gpt-6.1-sol",
                "reasoning_effort": "high",
                "timeout_minutes": 90,
            }
        )
        phases.append(
            {
                "name": f"phase-{index}",
                "short_name": f"Phase {index}",
                "plan_path": "docs/plans/sample-plan.md",
                "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                "commit_message": "accepted first" if index == 1 else None,
                "cursor": phase_cursor,
                "codex": {"review_model": "gpt-6.1-sol", "review_reasoning_effort": "high"},
                "implementer": implementer,
            }
        )
    manifest.write_text(
        yaml.safe_dump(
            {"schema_version": 4, "name": "mixed", "short_name": "Mixed v4", "phases": phases},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    state_root = isolated_xdg / "state/ai_dev_loop"
    result = prepare_sequence(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=state_root / "engine.sqlite3",
            artifact_root=state_root / "artifacts",
        )
    )
    store = SqliteSchedulerStore(state_root / "engine.sqlite3")
    with store.begin_read() as conn:
        prepared = store.load_validated_sequence_state(conn, result.sequence_id)
    original_state_payload = store.dump_sequence_state(prepared)[1]
    original_idempotency_key = prepared.idempotency_key  # type: ignore[union-attr]
    entries = prepared.definition.entries  # type: ignore[union-attr]
    assert prepared.definition.schema_version == 4  # type: ignore[union-attr]
    _validate_schema(
        prepared.definition.model_dump(mode="json"),  # type: ignore[union-attr]
        "scheduler-sequence-definition-v4.json",
    )
    _validate_schema(
        entries[0].model_dump(mode="json"),
        "scheduler-frozen-sequence-entry-v4.json",
    )
    assert [entry.implementer.provider for entry in entries] == ["cursor", "codex"]  # type: ignore[union-attr]
    context = build_sequence_run_context(
        definition=prepared.definition,  # type: ignore[union-attr]
        entry=entries[0],
        entry_hash=SqliteSchedulerStore.dump_sequence_entry(entries[0])[1],
    )
    assert context.schema_version == 10
    _validate_schema(
        context.model_dump(mode="json"),
        "scheduler-submitted-run-context-v10.json",
    )
    assert context.sequence is not None and context.sequence.ordinal == 1

    # A later source-manifest edit cannot rewrite the accepted frozen definition,
    # while changing an authority-bearing SDK parameter produces new identity.
    changed_phases = [dict(phase) for phase in phases]
    changed_phases[0]["cursor"] = {
        **phase_cursor,
        "parameters": {
            "context": "256k",
            "reasoning_effort": "high",
            "fast": True,
        },
    }
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 4,
                "name": "mixed",
                "short_name": "Mixed v4",
                "phases": changed_phases,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    changed = prepare_sequence(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=state_root / "engine.sqlite3",
            artifact_root=state_root / "artifacts",
        )
    )
    assert changed.sequence_id != result.sequence_id
    with store.begin_read() as conn:
        original_reloaded = store.load_validated_sequence_state(conn, result.sequence_id)
        changed_prepared = store.load_validated_sequence_state(conn, changed.sequence_id)
    assert store.dump_sequence_state(original_reloaded)[1] == original_state_payload
    assert original_reloaded.idempotency_key == original_idempotency_key  # type: ignore[union-attr]
    assert changed_prepared.idempotency_key != original_idempotency_key  # type: ignore[union-attr]

    started = start_sequence(result.sequence_id, db_path=state_root / "engine.sqlite3")
    tick = TickService(
        store,
        ProtectedArtifactStore(state_root / "artifacts"),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 17, 13, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase252-mixed-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    final_id = entries[1].planned_run_id
    for _ in range(100):
        tick.run_once()
        with store.begin_read() as conn:
            first, _, _ = store.load_validated_snapshot(conn, started.run_id)
            final_row = conn.execute(
                "SELECT 1 FROM scheduler_runs WHERE run_id=?", (final_id,)
            ).fetchone()
            final = (
                store.load_validated_snapshot(conn, final_id)[0] if final_row is not None else None
            )
        assert first.kind != "blocked", getattr(first, "block_reason_summary", "")
        if final is not None:
            assert final.kind != "blocked", getattr(final, "block_reason_summary", "")
            if final.kind == "completed":
                break
    assert first.kind == "completed"
    assert final is not None and final.kind == "completed"
    assert first.context.implementer.provider == "cursor"  # type: ignore[union-attr]
    assert final.context.implementer.provider == "codex"  # type: ignore[union-attr]
    assert first.cursor.sdk is not None
    assert final.codex.implementer_session_id == "019abc00-1111-4222-8333-888888888888"
    assert final.codex.reviewer_session_id == "019abc00-1111-4222-8333-777777777777"
    assert final.codex.implementer_session_id != final.codex.reviewer_session_id
    log = subprocess.check_output(["git", "log", "--format=%s"], cwd=git_repo, text=True)
    assert "accepted first" in log
    assert (
        subprocess.check_output(
            ["git", "diff", "--cached", "--name-only"], cwd=git_repo, text=True
        ).strip()
        == "codex-final.txt"
    )


def test_manifest_v3_sdk_fields_fail_before_sequence_publication(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    _configure_v2(git_repo)
    phase = {
        "name": "sdk-phase",
        "short_name": "SDK phase",
        "plan_path": "docs/plans/sample-plan.md",
        "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
        "cursor": {
            "runtime": "sdk-local",
            "model": "grok-4.7",
            "parameters": {"context": "256k", "reasoning_effort": "high", "fast": False},
        },
        "codex": {"review_model": "gpt-6.1-sol", "review_reasoning_effort": "high"},
        "implementer": {"provider": "cursor"},
    }
    manifest = git_repo / "invalid-v3-sdk.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "name": "invalid sdk v3",
                "short_name": "Invalid SDK v3",
                "phases": [
                    {**phase, "commit_message": "invalid checkpoint"},
                    {**phase, "name": "sdk-final", "short_name": "SDK final"},
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    db = isolated_xdg / "state/ai_dev_loop/engine.sqlite3"

    with pytest.raises(ValidationError, match="manifest failed validation"):
        prepare_sequence(
            SequencePrepareOptions(
                manifest_path=manifest,
                repo_path=git_repo,
                db_path=db,
                artifact_root=isolated_xdg / "state/ai_dev_loop/artifacts",
            )
        )

    assert not db.exists()


def test_sdk_abort_fences_a_late_owned_turn_result(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    _configure_v2(git_repo)
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "abort baseline"], cwd=git_repo, check=True)
    fake_sdk = tmp_path / "fake-sdk-abort"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk))
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(
            SubmitOptions(
                short_name="SDK abort fence",
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_SESSION,
                codex_review_model="gpt-6.1-sol",
                codex_review_reasoning_effort="high",
                db_path=db,
                artifact_root=artifacts_root,
            )
        ).run_id
    start_run(run_id, db_path=db)
    store = SqliteSchedulerStore(db)
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 17, 15, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase252-abort-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=backend,
        preflight_port=OkPreflightPort(),
    )
    turn_request = None
    for _ in range(30):
        tick.run_once()
        for request in backend.launch_calls:
            if "--effect-kind" not in request.agent_argv:
                continue
            kind_index = request.agent_argv.index("--effect-kind")
            if request.agent_argv[kind_index + 1] == "cursor.run_turn":
                turn_request = request
                backend.set_scenario(
                    request.attempt_id,
                    FakeAttemptScenario(active_ticks=100, exit_code=0),
                )
                break
        if turn_request is not None:
            break
    assert turn_request is not None
    aborted = scheduler_abort_run(
        run_id,
        db_path=db,
        artifact_root=artifacts_root,
        backend=backend,
    )
    assert aborted.state_kind == "aborted" and aborted.abort_persisted is True

    # Simulate a result arriving after durable abort. The real backend stops the
    # complete control group; this deliberately adversarial write must remain inert.
    assert backend._execute_cursor_attempt(turn_request) == 0
    for _ in range(3):
        tick.run_once()
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        adopted = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events WHERE run_id=? AND event_kind='cursor_sdk_turn_completed'",
            (run_id,),
        ).fetchone()[0]
        downstream = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_attempts
            WHERE run_id=? AND component IN ('staging','codex')
            """,
            (run_id,),
        ).fetchone()[0]
    assert state.kind == "aborted" and adopted == 0 and downstream == 0
    assert subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=git_repo).returncode == 0
    cancelled_output = IntegrationProcessOutputReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    ).read_process_output(
        run_id,
        attempt_id=turn_request.attempt_id,
        stream="stdout",
    )
    assert cancelled_output.available is False
    assert cancelled_output.reason == "not_captured"

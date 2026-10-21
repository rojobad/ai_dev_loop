"""Offline checks for the pinned SDK's public native SQLite surfaces."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ai_dev_loop.integrations.cursor_sdk.adapter import (
    resolve_cursor_sdk_native_bridge_command,
)


def _sdk_layout() -> tuple[Path, Path, Path]:
    distribution = importlib.metadata.distribution("cursor-sdk")
    package_root = Path(distribution.locate_file("cursor_sdk")).resolve()
    bridge_root = package_root / "_vendor" / "bridge"
    node = bridge_root / "bin" / "node"
    assert node.is_file(), "pinned cursor-sdk extra must provide its bundled Node runtime"
    assert distribution.version == "1.0.37"
    return bridge_root, node, bridge_root / "node_modules/@cursor/sdk/dist/esm/sqlite.js"


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_public_sqlite_store_reopens_identical_blobs_and_isolates_roots(tmp_path: Path) -> None:
    bridge_root, node, sqlite_module = _sdk_layout()
    first_root = tmp_path / "owner-one"
    second_root = tmp_path / "owner-two"
    aggregate = first_root / "aggregate-checkpoints.jsonl"
    first_root.mkdir()
    aggregate.write_bytes(b"not-json-and-must-remain-untouched\n")
    original_aggregate = aggregate.read_bytes()
    script = r"""
        import { createHash } from 'node:crypto';

        const { SqliteLocalAgentStore } = await import(process.env.TEST_SQLITE_MODULE);
        const root = process.env.TEST_STORE_ROOT;
        const workspace = process.env.TEST_WORKSPACE;
        const agentId = process.env.TEST_AGENT;
        const expected = new Map();
        let store = await SqliteLocalAgentStore.open({ workspaceRef: workspace, stateRoot: root });
        await store.agents.create({agent: {
          agentId, cwd: workspace, status: 'idle', createdAt: 1, updatedAt: 1,
        }});
        for (let index = 0; index < 96; index += 1) {
          const data = new Uint8Array((index + 1) * 257);
          data.fill((index * 17) % 251);
          const blobId = createHash('sha256').update(data).digest('hex');
          expected.set(blobId, blobId);
          await store.checkpoints.create({agentId, blobId, data});
        }
        await store.dispose();
        store = await SqliteLocalAgentStore.open({ workspaceRef: workspace, stateRoot: root });
        const listed = await store.checkpoints.list({filter: {agentIds: [agentId], limit: 200}});
        if (listed.items.length !== expected.size) throw new Error('checkpoint count changed');
        for (const [blobId, digest] of expected) {
          const data = await store.checkpoints.get({agentId, blobId});
          if (data === null || createHash('sha256').update(data).digest('hex') !== digest) {
            throw new Error(`checkpoint bytes changed: ${blobId}`);
          }
        }
        const agent = await store.agents.get({agentId});
        const effectiveRoot = store.stateRoot;
        await store.dispose();
        process.stdout.write(JSON.stringify({root: effectiveRoot, count: listed.items.length, agentId: agent?.agentId}));
    """

    def exercise(root: Path, agent: str) -> dict[str, object]:
        completed = subprocess.run(
            [str(node), "--input-type=module", "-e", script],
            cwd=bridge_root / "node_modules",
            env={
                **os.environ,
                "TEST_STORE_ROOT": str(root),
                "TEST_WORKSPACE": str(tmp_path / "same-workspace"),
                "TEST_AGENT": agent,
                "TEST_SQLITE_MODULE": sqlite_module.as_uri(),
            },
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return json.loads(completed.stdout)

    first = exercise(first_root, "019abc00-1111-4222-8333-444444444444")
    second = exercise(second_root, "019abc00-1111-4222-8333-555555555555")
    assert first["count"] == second["count"] == 96
    assert first["root"] == str(first_root)
    assert second["root"] == str(second_root)
    assert (first_root / "index.db").is_file()
    assert (second_root / "index.db").is_file()
    assert list(first_root.glob("agents/*/store.db"))
    assert list(second_root.glob("agents/*/store.db"))
    assert aggregate.read_bytes() == original_aggregate
    assert not list(first_root.glob("*.ndjson"))


def test_default_python_bridge_route_characterizes_upstream_default_store(
    tmp_path: Path,
) -> None:
    """Keep the upstream default route as a control for the production command."""

    bridge_root, node, sqlite_module = _sdk_layout()
    home = tmp_path / "home"
    workspace = tmp_path / "same-workspace"
    owner_one = tmp_path / "owner-one"
    owner_two = tmp_path / "owner-two"
    home.mkdir()
    workspace.mkdir()
    owner_agents = (
        "019abc00-1111-4222-8333-444444444444",
        "019abc00-1111-4222-8333-555555555555",
    )
    default_agent = "019abc00-1111-4222-8333-666666666666"
    seed_script = r"""
        const { SqliteLocalAgentStore } = await import(process.env.TEST_SQLITE_MODULE);
        const options = {workspaceRef: process.env.TEST_WORKSPACE};
        if (process.env.TEST_STORE_ROOT) options.stateRoot = process.env.TEST_STORE_ROOT;
        const store = await SqliteLocalAgentStore.open(options);
        await store.agents.create({agent: {
          agentId: process.env.TEST_AGENT,
          cwd: process.env.TEST_WORKSPACE,
          status: 'idle',
          createdAt: 1,
          updatedAt: 1,
        }});
        const effectiveRoot = store.stateRoot;
        await store.dispose();
        process.stdout.write(JSON.stringify({root: effectiveRoot}));
    """
    base_environment = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "TEST_SQLITE_MODULE": sqlite_module.as_uri(),
        "TEST_WORKSPACE": str(workspace),
    }
    base_environment.pop("CURSOR_API_KEY", None)

    def seed(store_root: Path | None, agent_id: str) -> Path:
        completed = subprocess.run(
            [str(node), "--input-type=module", "-e", seed_script],
            cwd=bridge_root / "node_modules",
            env={
                **base_environment,
                "TEST_STORE_ROOT": str(store_root) if store_root is not None else "",
                "TEST_AGENT": agent_id,
            },
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return Path(json.loads(completed.stdout)["root"])

    assert seed(owner_one, owner_agents[0]) == owner_one
    assert seed(owner_two, owner_agents[1]) == owner_two
    default_root = seed(None, default_agent)
    before = {owner_one: _tree_hashes(owner_one), owner_two: _tree_hashes(owner_two)}

    bridge_script = r"""
import json
import os
from pathlib import Path
from cursor_sdk import CursorClient, LocalAgentOptions, LocalAgentStoreConfig

workspace = Path(os.environ["TEST_WORKSPACE"])
default_agent = os.environ["TEST_DEFAULT_AGENT"]
owner_agents = json.loads(os.environ["TEST_OWNER_AGENTS"])
observed = []
for index, requested_root in enumerate(json.loads(os.environ["TEST_REQUESTED_ROOTS"])):
    local = LocalAgentOptions(
        cwd=workspace,
        store=LocalAgentStoreConfig(type="sqlite"),
    )
    with CursorClient.launch_bridge(
        workspace=workspace,
        state_root=requested_root,
        local=local,
        timeout=30,
        client_timeout=30,
        max_retries=0,
    ) as client:
        listed = client.list_agents(cwd=workspace).to_dict()["items"]
        fetched = client.get_agent(default_agent, cwd=workspace)
        try:
            client.get_agent(owner_agents[index], cwd=workspace)
        except Exception as exc:
            owner_get_error = type(exc).__name__
        else:
            owner_get_error = None
        bridge = client._owned_bridge
        process = bridge.process
        observed.append({
            "requested_root": requested_root,
            "advertised_root": client._owned_bridge.endpoint.state_root,
            "listed_ids": [item["agent_id"] for item in listed],
            "fetched_id": fetched.agent_id,
            "owner_get_error": owner_get_error,
        })
    observed[-1]["bridge_returncode_after_close"] = process.poll()
print(json.dumps(observed))
    """
    completed = subprocess.run(
        [sys.executable, "-c", bridge_script],
        cwd=workspace,
        env={
            **base_environment,
            "TEST_DEFAULT_AGENT": default_agent,
            "TEST_OWNER_AGENTS": json.dumps([owner_agents[0], owner_agents[1], owner_agents[0]]),
            "TEST_REQUESTED_ROOTS": json.dumps([str(owner_one), str(owner_two), str(owner_one)]),
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    observed = json.loads(completed.stdout)

    assert [item["advertised_root"] for item in observed] == [
        str(owner_one),
        str(owner_two),
        str(owner_one),
    ]
    assert all(item["listed_ids"] == [default_agent] for item in observed)
    assert all(item["fetched_id"] == default_agent for item in observed)
    assert all(item["owner_get_error"] == "AgentNotFoundError" for item in observed)
    assert all(item["bridge_returncode_after_close"] is not None for item in observed)
    assert owner_agents[0] not in observed[0]["listed_ids"]
    assert owner_agents[1] not in observed[1]["listed_ids"]
    assert default_root.is_dir()
    assert _tree_hashes(owner_one) == before[owner_one]
    assert _tree_hashes(owner_two) == before[owner_two]


def test_production_native_bridge_isolates_owners_reopens_and_excludes_default(
    tmp_path: Path,
) -> None:
    """The production command must fail if it regresses to the default bridge route."""

    bridge_root, node, sqlite_module = _sdk_layout()
    home = tmp_path / "home"
    workspace = tmp_path / "same-workspace"
    owner_one = tmp_path / "owner-one"
    owner_two = tmp_path / "owner-two"
    home.mkdir()
    workspace.mkdir()
    owner_agents = (
        "019abc00-1111-4222-8333-777777777777",
        "019abc00-1111-4222-8333-888888888888",
    )
    default_agent = "019abc00-1111-4222-8333-999999999999"
    seed_script = r"""
        const { SqliteLocalAgentStore } = await import(process.env.TEST_SQLITE_MODULE);
        const options = {workspaceRef: process.env.TEST_WORKSPACE};
        if (process.env.TEST_STORE_ROOT) options.stateRoot = process.env.TEST_STORE_ROOT;
        const store = await SqliteLocalAgentStore.open(options);
        await store.agents.create({agent: {
          agentId: process.env.TEST_AGENT,
          cwd: process.env.TEST_WORKSPACE,
          status: 'idle',
          createdAt: 1,
          updatedAt: 1,
        }});
        const effectiveRoot = store.stateRoot;
        await store.dispose();
        process.stdout.write(JSON.stringify({root: effectiveRoot}));
    """
    base_environment = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "TEST_SQLITE_MODULE": sqlite_module.as_uri(),
        "TEST_WORKSPACE": str(workspace),
    }
    for name in tuple(base_environment):
        if name.startswith("CURSOR_") and ("KEY" in name or "TOKEN" in name):
            base_environment.pop(name)

    def seed(store_root: Path | None, agent_id: str) -> Path:
        completed = subprocess.run(
            [str(node), "--input-type=module", "-e", seed_script],
            cwd=bridge_root / "node_modules",
            env={
                **base_environment,
                "TEST_STORE_ROOT": str(store_root) if store_root is not None else "",
                "TEST_AGENT": agent_id,
            },
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return Path(json.loads(completed.stdout)["root"])

    assert seed(owner_one, owner_agents[0]) == owner_one
    assert seed(owner_two, owner_agents[1]) == owner_two
    default_root = seed(None, default_agent)
    default_before = _tree_hashes(default_root)
    command = resolve_cursor_sdk_native_bridge_command()
    assert Path(command[0]) == node
    assert command[1] == "--import"
    assert Path(command[2]).name == "native_store_initializer.mjs"
    assert Path(command[3]).name == "cursor-sdk-bridge.js"

    bridge_script = r"""
import json
import os
from pathlib import Path
from cursor_sdk import CursorClient, LocalAgentOptions, LocalAgentStoreConfig

workspace = Path(os.environ["TEST_WORKSPACE"])
owner_agents = json.loads(os.environ["TEST_OWNER_AGENTS"])
default_agent = os.environ["TEST_DEFAULT_AGENT"]
observed = []
for requested_root, expected_agent in zip(
    json.loads(os.environ["TEST_REQUESTED_ROOTS"]),
    json.loads(os.environ["TEST_EXPECTED_AGENTS"]),
):
    local = LocalAgentOptions(cwd=workspace, store=LocalAgentStoreConfig(type="sqlite"))
    with CursorClient.launch_bridge(
        command=json.loads(os.environ["TEST_PRODUCTION_COMMAND"]),
        workspace=workspace,
        state_root=requested_root,
        local=local,
        timeout=30,
        client_timeout=30,
        max_retries=0,
    ) as client:
        listed = [item.agent_id for item in client.list_agents(cwd=workspace)]
        fetched = client.get_agent(expected_agent, cwd=workspace).agent_id
        excluded = []
        for forbidden in set(owner_agents + [default_agent]) - {expected_agent}:
            try:
                client.get_agent(forbidden, cwd=workspace)
            except Exception as exc:
                excluded.append(type(exc).__name__)
            else:
                raise AssertionError("another owner or the default store was visible")
        observed.append({"listed": listed, "fetched": fetched, "excluded": excluded})
print(json.dumps(observed))
    """
    completed = subprocess.run(
        [sys.executable, "-c", bridge_script],
        cwd=workspace,
        env={
            **base_environment,
            "TEST_OWNER_AGENTS": json.dumps(list(owner_agents)),
            "TEST_DEFAULT_AGENT": default_agent,
            "TEST_REQUESTED_ROOTS": json.dumps([str(owner_one), str(owner_two), str(owner_one)]),
            "TEST_EXPECTED_AGENTS": json.dumps([owner_agents[0], owner_agents[1], owner_agents[0]]),
            "TEST_PRODUCTION_COMMAND": json.dumps(command),
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    observed = json.loads(completed.stdout)

    assert [item["listed"] for item in observed] == [
        [owner_agents[0]],
        [owner_agents[1]],
        [owner_agents[0]],
    ]
    assert [item["fetched"] for item in observed] == [
        owner_agents[0],
        owner_agents[1],
        owner_agents[0],
    ]
    assert all(
        item["excluded"] == ["AgentNotFoundError", "AgentNotFoundError"] for item in observed
    )
    assert _tree_hashes(default_root) == default_before
    assert (owner_one / "index.db").is_file()
    assert (owner_two / "index.db").is_file()


def test_production_native_bridge_open_failure_stops_before_agent_operations(
    tmp_path: Path,
) -> None:
    bridge_root, node, _ = _sdk_layout()
    del bridge_root, node
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "corrupt-owner"
    workspace.mkdir()
    state_root.mkdir()
    (state_root / "index.db").write_bytes(b"not a sqlite database")
    command = resolve_cursor_sdk_native_bridge_command()
    script = r"""
import json
import os
from pathlib import Path
from cursor_sdk import CursorClient, LocalAgentOptions, LocalAgentStoreConfig

workspace = Path(os.environ["TEST_WORKSPACE"])
local = LocalAgentOptions(cwd=workspace, store=LocalAgentStoreConfig(type="sqlite"))
try:
    CursorClient.launch_bridge(
        command=json.loads(os.environ["TEST_PRODUCTION_COMMAND"]),
        workspace=workspace,
        state_root=os.environ["TEST_STATE_ROOT"],
        local=local,
        timeout=10,
        client_timeout=10,
        max_retries=0,
    )
except Exception as exc:
    print(json.dumps({"failure": type(exc).__name__, "agent_operations": 0}))
else:
    raise AssertionError("corrupt native state unexpectedly launched the bridge")
    """
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "TEST_WORKSPACE": str(workspace),
        "TEST_STATE_ROOT": str(state_root),
        "TEST_PRODUCTION_COMMAND": json.dumps(command),
    }
    for name in tuple(environment):
        if name.startswith("CURSOR_") and ("KEY" in name or "TOKEN" in name):
            environment.pop(name)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=workspace,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    observed = json.loads(completed.stdout)
    assert observed["failure"] == "CursorSDKError"
    assert observed["agent_operations"] == 0
    assert (state_root / "index.db").read_bytes() == b"not a sqlite database"


def test_isolated_installed_wheel_resolves_initializer_and_launches_native_store(
    tmp_path: Path,
) -> None:
    """Build/install the wheel, then launch outside the checkout with no API key."""

    repository = Path(__file__).resolve().parents[2]
    wheel_directory = tmp_path / "wheel"
    install_root = tmp_path / "installed"
    outside = tmp_path / "outside"
    workspace = outside / "workspace"
    state_root = outside / "owner"
    wheel_directory.mkdir()
    outside.mkdir()
    workspace.mkdir()
    build_environment = {
        **os.environ,
        "TMPDIR": "/tmp",
        "TMP": "/tmp",
        "TEMP": "/tmp",
        "UV_CACHE_DIR": "/tmp/phase257-uv-cache",
    }
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(wheel_directory)],
        cwd=repository,
        env=build_environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    wheels = list(wheel_directory.glob("ai_dev_loop-*.whl"))
    assert len(wheels) == 1
    uv = shutil.which("uv")
    assert uv is not None, "focused packaging validation requires uv"
    subprocess.run(
        [uv, "pip", "install", "--target", str(install_root), "--no-deps", str(wheels[0])],
        cwd=outside,
        env=build_environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    bridge_root, node, sqlite_module = _sdk_layout()
    agent_id = "019abc00-1111-4222-8333-aaaaaaaaaaaa"
    seed_script = r"""
const { SqliteLocalAgentStore } = await import(process.env.TEST_SQLITE_MODULE);
const store = await SqliteLocalAgentStore.open({
  workspaceRef: process.env.TEST_WORKSPACE,
  stateRoot: process.env.TEST_STATE_ROOT,
});
await store.agents.create({agent: {
  agentId: process.env.TEST_AGENT,
  cwd: process.env.TEST_WORKSPACE,
  status: 'idle',
  createdAt: 1,
  updatedAt: 1,
}});
await store.dispose();
    """
    subprocess.run(
        [str(node), "--input-type=module", "-e", seed_script],
        cwd=bridge_root / "node_modules",
        env={
            **build_environment,
            "TEST_SQLITE_MODULE": sqlite_module.as_uri(),
            "TEST_WORKSPACE": str(workspace),
            "TEST_STATE_ROOT": str(state_root),
            "TEST_AGENT": agent_id,
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    installed_script = r"""
import json
import os
from pathlib import Path
import ai_dev_loop
from ai_dev_loop.integrations.cursor_sdk.adapter import resolve_cursor_sdk_native_bridge_command
from cursor_sdk import CursorClient, LocalAgentOptions, LocalAgentStoreConfig

install_root = Path(os.environ["TEST_INSTALL_ROOT"]).resolve()
module_path = Path(ai_dev_loop.__file__).resolve()
command = resolve_cursor_sdk_native_bridge_command()
initializer = Path(command[2]).resolve()
workspace = Path(os.environ["TEST_WORKSPACE"])
local = LocalAgentOptions(cwd=workspace, store=LocalAgentStoreConfig(type="sqlite"))
with CursorClient.launch_bridge(
    command=command,
    workspace=workspace,
    state_root=os.environ["TEST_STATE_ROOT"],
    local=local,
    timeout=30,
    client_timeout=30,
    max_retries=0,
) as client:
    listed = [item.agent_id for item in client.list_agents(cwd=workspace)]
    fetched = client.get_agent(os.environ["TEST_AGENT"], cwd=workspace).agent_id
print(json.dumps({
    "module_installed": module_path.is_relative_to(install_root),
    "initializer_installed": initializer.is_relative_to(install_root),
    "listed": listed,
    "fetched": fetched,
}))
    """
    installed_environment = {
        **build_environment,
        "PYTHONPATH": str(install_root),
        "HOME": str(outside / "home"),
        "XDG_CONFIG_HOME": str(outside / "xdg-config"),
        "XDG_DATA_HOME": str(outside / "xdg-data"),
        "XDG_STATE_HOME": str(outside / "xdg-state"),
        "TEST_INSTALL_ROOT": str(install_root),
        "TEST_WORKSPACE": str(workspace),
        "TEST_STATE_ROOT": str(state_root),
        "TEST_AGENT": agent_id,
    }
    for name in tuple(installed_environment):
        if name.startswith("CURSOR_") and ("KEY" in name or "TOKEN" in name):
            installed_environment.pop(name)
    completed = subprocess.run(
        [sys.executable, "-c", installed_script],
        cwd=outside,
        env=installed_environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    observed = json.loads(completed.stdout)
    assert observed == {
        "module_installed": True,
        "initializer_installed": True,
        "listed": [agent_id],
        "fetched": agent_id,
    }

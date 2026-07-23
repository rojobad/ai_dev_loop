"""Owned LOCAL child process metadata for PR review v2 abort control."""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_dev_loop.launcher import read_process_pgid, read_process_starttime
from ai_dev_loop.locking import is_process_alive
from ai_dev_loop.paths import DIR_MODE, ensure_dir, set_sensitive_file_mode
from ai_dev_loop.pr_review_v2.infrastructure.paths import ensure_run_artifact_root
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SystemClock
from ai_dev_loop.pr_review_v2.workers.supervisor import process_group_alive

OwnedChildComponent = Literal["cursor", "codex"]
OWNED_CHILDREN_DIR = "local/owned-children"


@dataclass(frozen=True)
class OwnedChildMetadata:
    schema_version: int
    run_id: str
    component: OwnedChildComponent
    pid: int
    pgid: int
    process_start_time: str
    executable: str
    parent_pid: int
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "component": self.component,
            "pid": self.pid,
            "pgid": self.pgid,
            "process_start_time": self.process_start_time,
            "executable": self.executable,
            "parent_pid": self.parent_pid,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OwnedChildMetadata:
        component = str(payload["component"])
        if component not in {"cursor", "codex"}:
            raise ValueError("invalid owned child component")
        return cls(
            schema_version=int(payload["schema_version"]),
            run_id=str(payload["run_id"]),
            component=component,  # type: ignore[arg-type]
            pid=int(payload["pid"]),
            pgid=int(payload["pgid"]),
            process_start_time=str(payload["process_start_time"]),
            executable=str(payload["executable"]),
            parent_pid=int(payload["parent_pid"]),
            created_at=str(payload["created_at"]),
        )


class OwnedChildStore:
    """Persist/validate Cursor/Codex children owned by a v2 run."""

    def __init__(self, artifact_root: Path) -> None:
        self._root = artifact_root

    def _dir(self, run_id: str) -> Path:
        return ensure_run_artifact_root(self._root, run_id) / OWNED_CHILDREN_DIR

    def path_for(self, run_id: str, component: OwnedChildComponent) -> Path:
        return self._dir(run_id) / f"{component}.json"

    def write(self, metadata: OwnedChildMetadata) -> Path:
        path = self.path_for(metadata.run_id, metadata.component)
        ensure_dir(path.parent, mode=DIR_MODE)
        payload = json.dumps(
            metadata.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        set_sensitive_file_mode(tmp)
        os.replace(tmp, path)
        set_sensitive_file_mode(path)
        return path

    def read(self, run_id: str, component: OwnedChildComponent) -> OwnedChildMetadata | None:
        path = self.path_for(run_id, component)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return OwnedChildMetadata.from_dict(payload)
        except (KeyError, TypeError, ValueError):
            return None

    def clear(self, run_id: str, component: OwnedChildComponent) -> None:
        path = self.path_for(run_id, component)
        if path.is_file():
            path.unlink()

    def list_for_run(self, run_id: str) -> list[OwnedChildMetadata]:
        found: list[OwnedChildMetadata] = []
        for component in ("cursor", "codex"):
            meta = self.read(run_id, component)
            if meta is not None:
                found.append(meta)
        return found


def _read_process_executable(pid: int) -> str | None:
    if pid <= 0:
        return None
    try:
        return str(Path(f"/proc/{pid}/exe").resolve())
    except OSError:
        return None


def validate_owned_child(
    metadata: OwnedChildMetadata,
    *,
    run_id: str,
) -> bool:
    if metadata.schema_version != 1:
        return False
    if metadata.run_id != run_id:
        return False
    if metadata.pid <= 0 or metadata.pgid <= 0:
        return False
    if not metadata.process_start_time.strip():
        return False
    try:
        int(metadata.process_start_time)
    except ValueError:
        return False
    return bool(metadata.executable.strip())


def validate_owned_child_against_os(
    metadata: OwnedChildMetadata,
    *,
    run_id: str,
) -> bool:
    if not validate_owned_child(metadata, run_id=run_id):
        return False
    if not is_process_alive(metadata.pid):
        return False
    current_pgid = read_process_pgid(metadata.pid)
    if current_pgid is None or current_pgid != metadata.pgid:
        return False
    current_start = read_process_starttime(metadata.pid)
    if current_start is None:
        return False
    try:
        recorded_start = int(metadata.process_start_time)
    except ValueError:
        return False
    if current_start != recorded_start:
        return False
    exe = _read_process_executable(metadata.pid)
    if exe is None:
        return False
    return Path(exe).resolve() == Path(metadata.executable).resolve()


def register_owned_child(
    store: OwnedChildStore,
    *,
    run_id: str,
    component: OwnedChildComponent,
    pid: int,
    pgid: int,
    executable: str,
    parent_pid: int | None = None,
) -> OwnedChildMetadata:
    starttime = read_process_starttime(pid)
    if starttime is None:
        raise RuntimeError("failed to capture owned child start time")
    metadata = OwnedChildMetadata(
        schema_version=1,
        run_id=run_id,
        component=component,
        pid=pid,
        pgid=pgid,
        process_start_time=str(starttime),
        executable=executable,
        parent_pid=parent_pid if parent_pid is not None else os.getpid(),
        created_at=SystemClock().now().isoformat(),
    )
    if not validate_owned_child_against_os(metadata, run_id=run_id):
        raise RuntimeError("owned child failed OS ownership validation")
    store.write(metadata)
    return metadata


def signal_owned_child(
    metadata: OwnedChildMetadata,
    *,
    run_id: str,
    grace_seconds: float = 2.0,
) -> str:
    """Terminate an exactly validated owned child process group.

    Returns ``terminated``, ``unnecessary``, or ``refused``.
    """

    if not validate_owned_child_against_os(metadata, run_id=run_id):
        if validate_owned_child(metadata, run_id=run_id) and (
            not is_process_alive(metadata.pid) and not process_group_alive(metadata.pgid)
        ):
            return "unnecessary"
        return "refused"
    try:
        os.killpg(metadata.pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "unnecessary"
    except PermissionError:
        return "refused"
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not process_group_alive(metadata.pgid):
            return "terminated"
        time.sleep(0.05)
    try:
        os.killpg(metadata.pgid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except PermissionError:
        return "refused"
    return "terminated"


__all__ = [
    "OWNED_CHILDREN_DIR",
    "OwnedChildMetadata",
    "OwnedChildStore",
    "register_owned_child",
    "signal_owned_child",
    "validate_owned_child",
    "validate_owned_child_against_os",
]

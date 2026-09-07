"""Shared pytest fixtures."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run

FIXTURE_REPO = Path(__file__).resolve().parent / "fixtures" / "sample_repo"


def native_linux_temp_root() -> Path:
    """Prefer a native Linux temp root so WSL tests avoid DrvFS /mnt/c temp dirs."""

    for candidate in (Path("/tmp"), Path("/dev/shm")):
        if candidate.is_dir() and not str(candidate.resolve()).startswith("/mnt/"):
            return candidate
    default = Path(tempfile.gettempdir()).resolve()
    if str(default).startswith("/mnt/"):
        pytest.fail(
            "Tests require a native Linux temp directory. "
            "Set TMPDIR=/tmp (or use a native WSL filesystem temp path)."
        )
    return default


@pytest.fixture
def hermetic_tmp_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Temporary directory on a native Linux filesystem."""

    root = native_linux_temp_root()
    for variable in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(variable, str(root))
    path = Path(tempfile.mkdtemp(prefix="ai_dev_loop_hermetic_", dir=root))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def hermetic_home(hermetic_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = hermetic_tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def hermetic_xdg(hermetic_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = hermetic_tmp_path / "xdg"
    config = base / "config"
    state = base / "state"
    cache = base / "cache"
    for path in (config, state, cache):
        path.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    return base


@pytest.fixture
def hermetic_codex_env(hermetic_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Native WSL CODEX_HOME for bridge tests, overriding inherited DrvFS values."""

    codex_home = hermetic_home / ".codex"
    (codex_home / "sessions").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return codex_home


@pytest.fixture
def propagated_windows_env(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    """Simulate Windows-propagated TMP and CODEX_HOME values on WSL."""

    values = {
        "TMPDIR": "/mnt/c/Users/WinUser/AppData/Local/Temp",
        "TMP": "/mnt/c/Users/WinUser/AppData/Local/Temp",
        "TEMP": "/mnt/c/Users/WinUser/AppData/Local/Temp",
        "CODEX_HOME": "/mnt/c/Users/WinUser/.codex",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


@pytest.fixture
def hermetic_desktop_cli_env(
    propagated_windows_env: dict[str, str],
    hermetic_codex_env: Path,
) -> Path:
    """Native CODEX_HOME for CLI tests after simulating propagated DrvFS env."""

    return hermetic_codex_env


def chmod_supported(directory: Path) -> bool:
    if os.name == "nt":
        return False
    probe = directory / ".chmod-probe"
    probe.mkdir(exist_ok=True)
    try:
        os.chmod(probe, 0o700)
        return stat.S_IMODE(probe.stat().st_mode) == 0o700
    except OSError:
        return False
    finally:
        if probe.exists():
            probe.rmdir()


@pytest.fixture
def permission_test_root() -> Iterator[Path]:
    if os.name == "nt":
        pytest.skip("permission bits are not enforced on Windows")
    for base_dir in (Path("/tmp"), Path(tempfile.gettempdir())):
        if not base_dir.is_dir():
            continue
        root = Path(tempfile.mkdtemp(prefix="ai_dev_loop_perm_", dir=base_dir))
        if chmod_supported(root):
            yield root
            shutil.rmtree(root, ignore_errors=True)
            return
        shutil.rmtree(root, ignore_errors=True)
    pytest.skip("no filesystem available that enforces Unix permission bits")


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "xdg"
    config = base / "config"
    state = base / "state"
    cache = base / "cache"
    for path in (config, state, cache):
        path.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    return base


@pytest.fixture
def isolated_integrations(isolated_home: Path, isolated_xdg: Path) -> Path:
    return isolated_home


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    for relative in (
        "ai_dev_loop.yaml",
        "docs/plans/sample-plan.md",
        "docs/plans/prompt_sample-plan.txt",
    ):
        source = FIXTURE_REPO / relative
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial fixture")
    return repo


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


DEFAULT_FIXTURE_SESSION_ID = "019abc00-0000-0000-0000-000000000000"
SENSITIVE_SENTINEL = "SENSITIVE_SENTINEL_TRANSCRIPT_CONTENT_NEVER_PERSIST"


def write_session_rollout(
    sessions_dir: Path,
    *,
    session_id: str = DEFAULT_FIXTURE_SESSION_ID,
    model: str = "gpt-5.6-sol",
    reasoning_effort: str = "high",
    timestamp: str = "2026-07-10T12-00-00",
    include_transcript_sentinel: bool = True,
    extra_lines: list[str] | None = None,
    desktop_production_shape: bool = False,
) -> Path:
    """Write a sanitized Codex rollout JSONL for tests.

    When ``desktop_production_shape`` is True, emit the allowlisted Desktop fields
    observed in real rollouts: top-level ``turn_context.effort`` and
    ``thread_settings_applied.thread_settings.reasoning_effort``.
    """

    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"rollout-{timestamp}-{session_id}.jsonl"
    if desktop_production_shape:
        lines = [
            json.dumps(
                {
                    "timestamp": "2026-07-10T12:00:00.000Z",
                    "type": "session_meta",
                    "payload": {"id": session_id, "cwd": "/tmp/fixture-repo"},
                }
            ),
            json.dumps(
                {
                    "timestamp": "2026-07-10T12:00:01.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "thread_settings": {
                            "model": model,
                            "reasoning_effort": reasoning_effort,
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "timestamp": "2026-07-10T12:00:02.000Z",
                    "type": "turn_context",
                    "payload": {
                        "model": model,
                        "effort": reasoning_effort,
                    },
                }
            ),
        ]
    else:
        lines = [
            json.dumps(
                {
                    "timestamp": "2026-07-10T12:00:00.000Z",
                    "type": "session_meta",
                    "payload": {"id": session_id, "cwd": "/tmp/fixture-repo"},
                }
            ),
            json.dumps(
                {
                    "timestamp": "2026-07-10T12:00:01.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "model": model,
                        "reasoning_effort": reasoning_effort,
                    },
                }
            ),
            json.dumps(
                {
                    "timestamp": "2026-07-10T12:00:02.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "turn_context",
                        "model": model,
                        "reasoning_effort": reasoning_effort,
                    },
                }
            ),
        ]
    if include_transcript_sentinel:
        lines.append(
            json.dumps(
                {
                    "timestamp": "2026-07-10T12:00:03.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": SENSITIVE_SENTINEL}],
                    },
                }
            )
        )
    if extra_lines:
        lines.extend(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def fake_clis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agent_log = tmp_path / "agent.log"
    codex_log = tmp_path / "codex.log"
    agent_version_file = tmp_path / "agent_version.txt"
    codex_version_file = tmp_path / "codex_version.txt"
    agent_models_file = tmp_path / "agent_models.txt"
    codex_models_file = tmp_path / "codex_models.json"
    agent_models_file.write_text("composer-2.5-fast\nauto\n", encoding="utf-8")
    codex_models_file.write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.6-sol"},
                    {"id": "gpt-5.5"},
                    {"id": "o4-mini"},
                    {"id": "prepared-model"},
                ]
            }
        ),
        encoding="utf-8",
    )

    agent_script = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import json
        import os
        import sys
        import time

        args = sys.argv[1:]
        log_path = {repr(str(agent_log))}

        def log(message):
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        if not args:
            sys.exit(1)
        if args[0] == "--version":
            version_file = os.environ.get("FAKE_AGENT_VERSION_FILE")
            if version_file and os.path.isfile(version_file):
                with open(version_file, encoding="utf-8") as handle:
                    print(handle.read().strip())
            else:
                print(os.environ.get("FAKE_AGENT_VERSION", "agent 1.0.0"))
            sys.exit(0)
        if args[0] == "update":
            log("UPDATE")
            if os.environ.get("FAKE_AGENT_UPDATE_FAIL") == "1":
                print("agent update failed", file=sys.stderr)
                sys.exit(2)
            if os.environ.get("FAKE_AGENT_UPDATE_SLEEP"):
                time.sleep(float(os.environ["FAKE_AGENT_UPDATE_SLEEP"]))
            models_after = os.environ.get("FAKE_AGENT_UPDATE_MODELS")
            models_file = os.environ.get("FAKE_AGENT_MODELS_FILE")
            if models_after and models_file:
                with open(models_file, "w", encoding="utf-8") as handle:
                    handle.write(models_after)
            version_after = os.environ.get("FAKE_AGENT_VERSION_AFTER")
            version_file = os.environ.get("FAKE_AGENT_VERSION_FILE")
            if version_after and version_file:
                with open(version_file, "w", encoding="utf-8") as handle:
                    handle.write(version_after)
            print("agent updated")
            sys.exit(0)
        if args[0] == "create-chat":
            chat_id = os.environ.get("FAKE_AGENT_CHAT_ID", "019abc00-1111-2222-3333-444444444444")
            create_mode = os.environ.get("FAKE_AGENT_CREATE_CHAT_MODE", "success")
            log("ARGS:" + repr(args))
            log("CREATE_CHAT:" + chat_id)
            if create_mode == "fail":
                print("create-chat failed", file=sys.stderr)
                sys.exit(2)
            if create_mode == "sleep":
                ready = os.environ.get("FAKE_AGENT_CREATE_CHAT_READY")
                if ready:
                    with open(ready, "w", encoding="utf-8") as handle:
                        handle.write(f"{{os.getpid()}}\\n{{os.getpgid(0)}}\\n")
                time.sleep(float(os.environ.get("FAKE_AGENT_CREATE_CHAT_SLEEP_SECONDS", "60")))
            print(chat_id)
            sys.exit(0)
        if args[0] == "status":
            auth = os.environ.get("FAKE_AGENT_AUTH", "true")
            print(json.dumps({{"authenticated": auth == "true"}}))
            sys.exit(0 if auth == "true" else 1)
        if args[0] == "models":
            if "FAKE_AGENT_MODELS" in os.environ:
                model = os.environ.get("FAKE_AGENT_MODELS", "composer-2.5-fast")
            else:
                models_file = os.environ.get("FAKE_AGENT_MODELS_FILE")
                if models_file and os.path.isfile(models_file):
                    with open(models_file, encoding="utf-8") as handle:
                        model = handle.read().strip() or "composer-2.5-fast"
                else:
                    model = "composer-2.5-fast"
            print(model)
            sys.exit(0)
        if "-p" in args:
            log("ARGS:" + repr(args))
            sequence = os.environ.get("FAKE_AGENT_RUN_SEQUENCE", "").strip()
            sequence_counter = None
            if sequence:
                counter_file = os.environ.get(
                    "FAKE_AGENT_RUN_COUNTER",
                    os.path.join(os.path.dirname(log_path), "agent_run_counter.txt"),
                )
                try:
                    with open(counter_file, encoding="utf-8") as handle:
                        sequence_counter = int(handle.read().strip() or "0")
                except (OSError, ValueError):
                    sequence_counter = 0
                modes = [item.strip() for item in sequence.split(",") if item.strip()]
                mode = modes[min(sequence_counter, len(modes) - 1)]
                with open(counter_file, "w", encoding="utf-8") as handle:
                    handle.write(str(sequence_counter + 1))
            else:
                mode = os.environ.get("FAKE_AGENT_RUN_MODE", "success")
            modify_sequence = os.environ.get("FAKE_AGENT_MODIFY_SEQUENCE", "").strip()
            if modify_sequence and sequence_counter is not None:
                modify_modes = [item.strip() for item in modify_sequence.split(",") if item.strip()]
                modify_mode = modify_modes[min(sequence_counter, len(modify_modes) - 1)]
            else:
                modify_mode = os.environ.get("FAKE_AGENT_MODIFY_MODE", "tracked")
            if mode == "sleep":
                time.sleep(float(os.environ.get("FAKE_AGENT_SLEEP_SECONDS", "5")))
                sys.exit(0)
            if mode == "fail":
                print("agent failed", file=sys.stderr)
                sys.exit(2)
            if mode == "usage_limit":
                if modify_mode != "none" and "--workspace" in args:
                    workspace = args[args.index("--workspace") + 1]
                    if modify_mode == "tracked":
                        target = os.path.join(workspace, "ai_dev_loop.yaml")
                        with open(target, "a", encoding="utf-8") as handle:
                            handle.write("\\n# modified by fake agent before usage limit\\n")
                    elif modify_mode == "untracked":
                        target = os.path.join(workspace, "new_feature.txt")
                        with open(target, "w", encoding="utf-8") as handle:
                            handle.write("partial feature\\n")
                    elif modify_mode == "partial_both":
                        tracked = os.path.join(workspace, "ai_dev_loop.yaml")
                        with open(tracked, "a", encoding="utf-8") as handle:
                            handle.write("\\n# partial tracked\\n")
                        untracked = os.path.join(workspace, "partial_untracked.txt")
                        with open(untracked, "w", encoding="utf-8") as handle:
                            handle.write("partial untracked\\n")
                    elif modify_mode == "correction_stage":
                        import subprocess

                        target = os.path.join(workspace, "correction_feature.txt")
                        with open(target, "w", encoding="utf-8") as handle:
                            handle.write("correction staged\\n")
                        subprocess.run(
                            ["git", "add", "correction_feature.txt"],
                            cwd=workspace,
                            check=False,
                        )
                print(
                    "ActionRequiredError: You've hit your usage limit for this model. "
                    "Switch to Auto or another model to continue.",
                    file=sys.stderr,
                )
                sys.exit(2)
            if mode == "usage_limit_structured":
                if modify_mode != "none" and "--workspace" in args:
                    workspace = args[args.index("--workspace") + 1]
                    if modify_mode == "partial_both":
                        tracked = os.path.join(workspace, "ai_dev_loop.yaml")
                        with open(tracked, "a", encoding="utf-8") as handle:
                            handle.write("\\n# partial tracked\\n")
                        untracked = os.path.join(workspace, "partial_untracked.txt")
                        with open(untracked, "w", encoding="utf-8") as handle:
                            handle.write("partial untracked\\n")
                structured = (
                    "ActionRequiredError: You've hit your usage limit for this model. "
                    "Switch to Auto or another model to continue. "
                    "Resets on 2026-08-01. Billing amount: $20."
                )
                print(json.dumps({{"type": "error", "message": structured}}))
                sys.exit(2)
            if mode == "unparseable":
                print("not-json")
                sys.exit(0)
            if modify_mode != "none" and "--workspace" in args:
                workspace = args[args.index("--workspace") + 1]
                if modify_mode == "tracked":
                    target = os.path.join(workspace, "ai_dev_loop.yaml")
                    with open(target, "a", encoding="utf-8") as handle:
                        handle.write("\\n# modified by fake agent\\n")
                elif modify_mode == "untracked":
                    target = os.path.join(workspace, "new_feature.txt")
                    with open(target, "w", encoding="utf-8") as handle:
                        handle.write("new feature\\n")
                elif modify_mode == "partial_both":
                    tracked = os.path.join(workspace, "ai_dev_loop.yaml")
                    with open(tracked, "a", encoding="utf-8") as handle:
                        handle.write("\\n# partial tracked\\n")
                    untracked = os.path.join(workspace, "partial_untracked.txt")
                    with open(untracked, "w", encoding="utf-8") as handle:
                        handle.write("partial untracked\\n")
                elif modify_mode == "stage_self":
                    import subprocess

                    target = os.path.join(workspace, "staged_by_agent.txt")
                    with open(target, "w", encoding="utf-8") as handle:
                        handle.write("staged by agent\\n")
                    subprocess.run(["git", "add", "staged_by_agent.txt"], cwd=workspace, check=False)
                elif modify_mode == "modify_prompt":
                    target = os.path.join(workspace, "docs/plans/prompt_sample-plan.txt")
                    with open(target, "a", encoding="utf-8") as handle:
                        handle.write("\\nmodified prompt\\n")
                elif modify_mode == "correction":
                    target = os.path.join(workspace, "correction_feature.txt")
                    with open(target, "w", encoding="utf-8") as handle:
                        handle.write("correction feature\\n")
                elif modify_mode == "correction_stage":
                    import subprocess

                    target = os.path.join(workspace, "correction_feature.txt")
                    with open(target, "w", encoding="utf-8") as handle:
                        handle.write("correction staged\\n")
                    subprocess.run(["git", "add", "correction_feature.txt"], cwd=workspace, check=False)
                elif modify_mode == "correction_partial_stage":
                    import subprocess

                    staged = os.path.join(workspace, "correction_staged.txt")
                    unstaged = os.path.join(workspace, "ai_dev_loop.yaml")
                    with open(staged, "w", encoding="utf-8") as handle:
                        handle.write("staged correction\\n")
                    subprocess.run(["git", "add", "correction_staged.txt"], cwd=workspace, check=False)
                    with open(unstaged, "a", encoding="utf-8") as handle:
                        handle.write("\\n# unstaged correction\\n")
                elif modify_mode == "correction_ignore_generated":
                    import subprocess

                    ignore = os.path.join(workspace, ".gitignore")
                    with open(ignore, "a", encoding="utf-8") as handle:
                        handle.write("\\ngenerated.out\\n")
                    generated = os.path.join(workspace, "generated.out")
                    with open(generated, "w", encoding="utf-8") as handle:
                        handle.write("generated\\n")
                    subprocess.run(["git", "add", ".gitignore"], cwd=workspace, check=False)
                    subprocess.run(
                        ["git", "rm", "--cached", "-f", "generated.out"],
                        cwd=workspace,
                        check=False,
                    )
                elif modify_mode == "correction_commit_forbidden":
                    import subprocess

                    target = os.path.join(workspace, "forbidden.txt")
                    with open(target, "w", encoding="utf-8") as handle:
                        handle.write("should not commit\\n")
                    subprocess.run(["git", "add", "forbidden.txt"], cwd=workspace, check=False)
                    subprocess.run(
                        ["git", "commit", "-m", "forbidden cursor commit"],
                        cwd=workspace,
                        check=False,
                    )
                elif modify_mode == "modify_plan":
                    target = os.path.join(workspace, "docs/plans/sample-plan.md")
                    with open(target, "w", encoding="utf-8") as handle:
                        handle.write("# modified plan\\n")
            prompt = args[-1]
            print(json.dumps({{"type": "result", "result": f"done: {{prompt[:32]}}"}}))
            sys.exit(0)
        sys.exit(1)
        """
    )
    codex_script = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import json
        import os
        import sys
        import time

        args = sys.argv[1:]
        log_path = {repr(str(codex_log))}

        def log(message):
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(message + "\\n")

        if args and args[0] == "--version":
            version_file = os.environ.get("FAKE_CODEX_VERSION_FILE")
            if version_file and os.path.isfile(version_file):
                with open(version_file, encoding="utf-8") as handle:
                    print(handle.read().strip())
            else:
                print(os.environ.get("FAKE_CODEX_VERSION", "codex-cli 0.142.5"))
            sys.exit(0)

        if args and args[0] == "update":
            log("UPDATE")
            if os.environ.get("FAKE_CODEX_UPDATE_FAIL") == "1":
                print("codex update failed", file=sys.stderr)
                sys.exit(2)
            if os.environ.get("FAKE_CODEX_UPDATE_SLEEP"):
                time.sleep(float(os.environ["FAKE_CODEX_UPDATE_SLEEP"]))
            models_after = os.environ.get("FAKE_CODEX_UPDATE_MODELS")
            models_file = os.environ.get("FAKE_CODEX_MODELS_FILE")
            if models_after and models_file:
                with open(models_file, "w", encoding="utf-8") as handle:
                    handle.write(models_after)
            version_after = os.environ.get("FAKE_CODEX_VERSION_AFTER")
            version_file = os.environ.get("FAKE_CODEX_VERSION_FILE")
            if version_after and version_file:
                with open(version_file, "w", encoding="utf-8") as handle:
                    handle.write(version_after)
            print("codex updated")
            sys.exit(0)

        if len(args) >= 2 and args[0] == "debug" and args[1] == "models":
            models_file = os.environ.get("FAKE_CODEX_MODELS_FILE")
            if models_file and os.path.isfile(models_file):
                with open(models_file, encoding="utf-8") as handle:
                    raw = handle.read().strip()
            else:
                raw = os.environ.get(
                    "FAKE_CODEX_MODELS",
                    json.dumps({{"models": [{{"id": "gpt-5.6-sol"}}, {{"id": "gpt-5.5"}}]}}),
                )
            if os.environ.get("FAKE_CODEX_MODELS_FAIL") == "1":
                print("model catalog unavailable", file=sys.stderr)
                sys.exit(2)
            try:
                json.loads(raw)
                print(raw)
            except json.JSONDecodeError:
                models = [item.strip() for item in raw.split(",") if item.strip()]
                print(json.dumps({{"models": [{{"id": item}} for item in models]}}))
            sys.exit(0)

        if len(args) >= 2 and args[0] == "login" and args[1] == "status":
            auth = os.environ.get("FAKE_CODEX_AUTH", "true")
            if auth == "true":
                print("Logged in to Codex")
                sys.exit(0)
            print("Not logged in", file=sys.stderr)
            sys.exit(1)

        if len(args) >= 3 and args[0] == "exec" and args[1] == "--cd":
            log("ARGS:" + repr(args))
            stdin_prompt = sys.stdin.read()
            log("STDIN:" + stdin_prompt)
            sequence = os.environ.get("FAKE_CODEX_REVIEW_SEQUENCE", "").strip()
            if sequence:
                counter_file = os.environ.get(
                    "FAKE_CODEX_REVIEW_COUNTER",
                    os.path.join(os.path.dirname(log_path), "codex_review_counter.txt"),
                )
                try:
                    with open(counter_file, encoding="utf-8") as handle:
                        counter = int(handle.read().strip() or "0")
                except (OSError, ValueError):
                    counter = 0
                modes = [item.strip() for item in sequence.split(",") if item.strip()]
                mode = modes[min(counter, len(modes) - 1)]
                with open(counter_file, "w", encoding="utf-8") as handle:
                    handle.write(str(counter + 1))
            else:
                mode = os.environ.get("FAKE_CODEX_REVIEW_MODE", "no_findings")
            output_last_message = None
            if "--output-last-message" in args:
                output_last_message = args[args.index("--output-last-message") + 1]
                parent = os.path.dirname(output_last_message)
                if parent and not os.path.isdir(parent):
                    print(
                        f"output-last-message parent missing: {{parent}}",
                        file=sys.stderr,
                    )
                    sys.exit(91)
            if mode == "sleep":
                if "resume" not in args:
                    bootstrap_id = os.environ.get(
                        "FAKE_CODEX_BOOTSTRAP_SESSION_ID",
                        "019def00-0000-0000-0000-0000000000bb",
                    )
                    print(json.dumps({{"type": "thread.started", "thread_id": bootstrap_id}}), flush=True)
                    ready_path = os.environ.get("FAKE_CODEX_BOOTSTRAP_READY_PATH", "").strip()
                    if ready_path:
                        parent = os.path.dirname(ready_path)
                        if parent:
                            os.makedirs(parent, exist_ok=True)
                        with open(ready_path, "w", encoding="utf-8") as handle:
                            handle.write("ready\\n")
                        print("FAKE_CODEX_BOOTSTRAP_READY", flush=True)
                time.sleep(float(os.environ.get("FAKE_CODEX_SLEEP_SECONDS", "5")))
                sys.exit(0)
            if mode == "fail":
                if "resume" not in args:
                    bootstrap_id = os.environ.get(
                        "FAKE_CODEX_BOOTSTRAP_SESSION_ID",
                        "019def00-0000-0000-0000-0000000000bb",
                    )
                    print(json.dumps({{"type": "thread.started", "thread_id": bootstrap_id}}), flush=True)
                print("codex review failed", file=sys.stderr)
                sys.exit(2)
            if mode == "output_artifact_fail":
                if "resume" not in args:
                    bootstrap_id = os.environ.get(
                        "FAKE_CODEX_BOOTSTRAP_SESSION_ID",
                        "019def00-0000-0000-0000-0000000000bb",
                    )
                    print(json.dumps({{"type": "thread.started", "thread_id": bootstrap_id}}), flush=True)
                target = output_last_message or "codex/reviews/01.json"
                print(
                    f"failed to write output-last-message {{target}}: "
                    "No such file or directory (os error 2)",
                    file=sys.stderr,
                )
                sys.exit(2)
            if mode == "invalid_json":
                if output_last_message:
                    with open(output_last_message, "w", encoding="utf-8") as handle:
                        handle.write("not-json")
                print(json.dumps({{"type": "message", "content": "invalid"}}))
                sys.exit(0)
            if mode == "findings":
                result = {{
                    "has_actionable_findings": True,
                    "findings_count": 1,
                    "highest_severity": "P1",
                    "review_markdown": "# Review\\n\\nFound issue.",
                    "cursor_fix_prompt": "Fix the sample issue in ai_dev_loop.yaml.",
                    "tests_status": "skipped_findings_present",
                    "summary": "One actionable finding.",
                }}
            elif mode == "blocked_environment":
                result = {{
                    "has_actionable_findings": False,
                    "findings_count": 0,
                    "highest_severity": None,
                    "review_markdown": "# Review\\n\\nNo issues, tests blocked.",
                    "cursor_fix_prompt": None,
                    "tests_status": "blocked_environment",
                    "summary": "No findings; environment blocked tests.",
                }}
            else:
                result = {{
                    "has_actionable_findings": False,
                    "findings_count": 0,
                    "highest_severity": None,
                    "review_markdown": "# Review\\n\\nNo issues found.",
                    "cursor_fix_prompt": None,
                    "tests_status": "passed",
                    "summary": "No actionable findings.",
                }}
            if output_last_message:
                with open(output_last_message, "w", encoding="utf-8") as handle:
                    json.dump(result, handle)
            if "resume" not in args:
                bootstrap_id = os.environ.get(
                    "FAKE_CODEX_BOOTSTRAP_SESSION_ID",
                    "019def00-0000-0000-0000-0000000000bb",
                )
                print(json.dumps({{"type": "thread.started", "thread_id": bootstrap_id}}), flush=True)
            print(json.dumps({{"type": "message", "content": "review complete"}}))
            sys.exit(0)

        sys.exit(1)
        """
    )
    _write_executable(bin_dir / "agent", agent_script)
    _write_executable(bin_dir / "codex", codex_script)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_COUNTER", str(tmp_path / "codex_review_counter.txt"))
    monkeypatch.setenv("FAKE_AGENT_RUN_COUNTER", str(tmp_path / "agent_run_counter.txt"))
    monkeypatch.setenv("FAKE_AGENT_VERSION_FILE", str(agent_version_file))
    monkeypatch.setenv("FAKE_CODEX_VERSION_FILE", str(codex_version_file))
    monkeypatch.setenv("FAKE_AGENT_MODELS_FILE", str(agent_models_file))
    monkeypatch.setenv("FAKE_CODEX_MODELS_FILE", str(codex_models_file))
    return {
        "bin_dir": bin_dir,
        "agent_log": agent_log,
        "codex_log": codex_log,
        "codex_review_counter": tmp_path / "codex_review_counter.txt",
        "agent_models_file": agent_models_file,
        "codex_models_file": codex_models_file,
    }


@pytest.fixture
def fixture_codex_session(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic native Codex home with a sanitized default session rollout."""

    codex_home = isolated_home / ".codex"
    write_session_rollout(codex_home / "sessions")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return codex_home


@pytest.fixture
def prepared_run(
    git_repo: Path,
    isolated_xdg,
    isolated_home,
    fake_clis,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    codex_home = isolated_home / ".codex"
    sessions = codex_home / "sessions"
    write_session_rollout(sessions)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        result = prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id=DEFAULT_FIXTURE_SESSION_ID,
            )
        )
    from ai_dev_loop.paths import run_dir

    run_path = run_dir("fixture-project", result.run_id)
    return {"run_id": result.run_id, "run_path": run_path, "repo": git_repo}

"""Shared pytest fixtures."""

from __future__ import annotations

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


@pytest.fixture
def fake_clis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agent_log = tmp_path / "agent.log"
    codex_log = tmp_path / "codex.log"

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
        if args[0] == "create-chat":
            chat_id = os.environ.get("FAKE_AGENT_CHAT_ID", "019abc00-1111-2222-3333-444444444444")
            print(chat_id)
            sys.exit(0)
        if args[0] == "status":
            auth = os.environ.get("FAKE_AGENT_AUTH", "true")
            print(json.dumps({{"authenticated": auth == "true"}}))
            sys.exit(0 if auth == "true" else 1)
        if args[0] == "models":
            model = os.environ.get("FAKE_AGENT_MODELS", "composer-2.5-fast")
            print(model)
            sys.exit(0)
        if "-p" in args:
            log("ARGS:" + repr(args))
            mode = os.environ.get("FAKE_AGENT_RUN_MODE", "success")
            if mode == "sleep":
                time.sleep(float(os.environ.get("FAKE_AGENT_SLEEP_SECONDS", "5")))
                sys.exit(0)
            if mode == "fail":
                print("agent failed", file=sys.stderr)
                sys.exit(2)
            if mode == "unparseable":
                print("not-json")
                sys.exit(0)
            modify_mode = os.environ.get("FAKE_AGENT_MODIFY_MODE", "tracked")
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
            mode = os.environ.get("FAKE_CODEX_REVIEW_MODE", "no_findings")
            if mode == "sleep":
                time.sleep(float(os.environ.get("FAKE_CODEX_SLEEP_SECONDS", "5")))
                sys.exit(0)
            if mode == "fail":
                print("codex review failed", file=sys.stderr)
                sys.exit(2)
            output_last_message = None
            if "--output-last-message" in args:
                output_last_message = args[args.index("--output-last-message") + 1]
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
            print(json.dumps({{"type": "message", "content": "review complete"}}))
            sys.exit(0)

        sys.exit(1)
        """
    )
    _write_executable(bin_dir / "agent", agent_script)
    _write_executable(bin_dir / "codex", codex_script)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return {
        "bin_dir": bin_dir,
        "agent_log": agent_log,
        "codex_log": codex_log,
    }


@pytest.fixture
def prepared_run(git_repo: Path, isolated_xdg, fake_clis) -> dict[str, object]:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        result = prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id="019abc00-0000-0000-0000-000000000000",
            )
        )
    from ai_dev_loop.paths import run_dir

    run_path = run_dir("fixture-project", result.run_id)
    return {"run_id": result.run_id, "run_path": run_path, "repo": git_repo}

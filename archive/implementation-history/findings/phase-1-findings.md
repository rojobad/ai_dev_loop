# Phase 1 Findings

This file summarizes the Phase 1 implementation and review cycle. It is intended as a handoff artifact so Phase 2 can proceed without depending on prior chat history.

## Scope

Phase 1 built the package foundation and a real `prepare` command for `ai_dev_loop`.

It did not implement the automated Cursor/Codex execution loop. In particular, Phase 1 did not implement Cursor chat creation/execution, Codex review execution, staging after Cursor turns, `start`, `resume`, `abort`, global Codex skill installation, or SessionStart hook installation.

The implemented slice is enough to:

- install and run the Python CLI locally;
- validate repository configuration;
- create a durable prepared run from stdin prompt content;
- store all run state outside the target repository under XDG state;
- inspect/list/status/log prepared run artifacts;
- provide placeholders for later workflow commands.

## Environment And Tooling

- Workspace: `/home/rojobad/Projects/ai_dev_loop`
- Primary runtime workflow: `uv`
- Package runtime requirement: Python `>=3.11`
- The user prefers not to modify or replace system Python.
- A project `.venv` was created via `uv` with Python 3.11.
- `pipx install .` remains optional documentation only; `uv tool install .` is the preferred user-level install path.

Recommended development commands:

```bash
uv sync
uv run python -m pytest -q -s
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m mypy src
uv run python -m build
```

Note: In this Codex desktop environment, plain `uv run python -m pytest -q` intermittently failed before collection with a pytest capture tempfile error. Running with `-s` completed successfully.

## Delivered Files And Structure

Phase 1 added the standard Python package structure:

```text
pyproject.toml
LICENSE
.gitignore
scripts/development-install.sh
src/ai_dev_loop/
tests/
uv.lock
```

Important package modules:

- `src/ai_dev_loop/cli.py`: Typer CLI and command tree.
- `src/ai_dev_loop/config.py`: config schema, YAML loading, precedence, validation.
- `src/ai_dev_loop/paths.py`: XDG path resolution and secure directory helpers.
- `src/ai_dev_loop/state.py`: run state models, hashes, atomic writes, status transitions.
- `src/ai_dev_loop/process.py`: direct subprocess runner foundation.
- `src/ai_dev_loop/runners/git.py`: Git discovery, path containment, worktree safety.
- `src/ai_dev_loop/commands/prepare.py`: full Phase 1 prepare workflow.
- `src/ai_dev_loop/run_discovery.py`: read-only run lookup helpers.
- `src/ai_dev_loop/schemas/`: JSON schemas for project config, run state, and Codex review result.

The package uses:

- Typer
- Pydantic v2
- PyYAML
- platformdirs
- hatchling
- pytest, ruff, mypy, build, types-PyYAML for development

The license is MIT.

## CLI Surface

The console entry point is:

```text
ai_dev_loop
```

Implemented in Phase 1:

- `ai_dev_loop prepare`
- `ai_dev_loop config validate`
- `ai_dev_loop status <run-id>`
- `ai_dev_loop list`
- `ai_dev_loop logs <run-id>`
- `ai_dev_loop inspect <run-id>`
- `ai_dev_loop doctor`
- `ai_dev_loop integrations status`

Present but intentionally not implemented yet:

- `ai_dev_loop start <run-id>`
- `ai_dev_loop resume <run-id>`
- `ai_dev_loop abort <run-id>`
- `ai_dev_loop integrations install`
- `ai_dev_loop integrations uninstall`

Later-phase placeholder commands return exit code `3`.

## Prepare Command Behavior

`prepare` is real and creates a durable prepared run without starting any agent.

Implemented behavior:

- Reads exact Cursor prompt content from stdin.
- Rejects TTY stdin and empty stdin.
- Resolves config precedence:
  1. CLI flags.
  2. Repository `ai_dev_loop.yaml`.
  3. Optional user-global config.
  4. Built-in defaults.
- Requires a valid repository config.
- Validates config version, project slug, executable/model fields, output format, sandbox values, positive timeouts, review iteration count, stage mode, and prompt filename template.
- Discovers Git root, common dir, worktree git dir, branch, HEAD, status, and staged files.
- Validates plan, prompt source, and explicit config paths are contained inside the repository and reject symlink escapes.
- Enforces clean-worktree safety when `workflow.require_clean_worktree` is true.
- Allows the selected plan file to be dirty.
- Allows the prompt source path to be dirty only when it is ignored and not tracked.
- Rejects pre-existing staged changes.
- Generates run IDs in the required format:

```text
<project-slug>-<UTC-basic-timestamp>-<short-random-id>
```

- Creates run artifacts under:

```text
$XDG_STATE_HOME/ai_dev_loop/runs/<project-slug>/<run-id>/
```

- Persists:
  - `state.json`
  - `manifest.json`
  - `effective-config.yaml`
  - `source-config.yaml`
  - `plan/plan.md`
  - `plan/metadata.json`
  - `prompts/cursor-initial.txt`
  - `git/baseline-status.txt`
  - `logs/ai_dev_loop.log`
- Stores SHA-256 hashes for plan, prompt, source config, effective config, and baseline status.
- Writes sensitive files with `0600` where the filesystem supports chmod.
- Creates app directories with `0700` where the filesystem supports chmod.
- Supports `--output json` with the expected `start_command` and `requires_codex_exit: true`.

Important detail: `source-config.yaml` is currently the normalized Pydantic/YAML representation of the source repo config, and its manifest hash is computed from that persisted artifact.

## Git Safety Details

Phase 1 uses direct subprocess argument arrays. It does not use `shell=True`.

The Git preflight uses:

```text
git rev-parse --show-toplevel
git rev-parse --git-common-dir
git rev-parse --git-dir
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git status --porcelain=v2 --untracked-files=all
git diff --cached --name-only
```

Review fixes made during Phase 1:

- Porcelain v2 parsing now handles tracked `1` records, rename/copy `2` records, unmerged `u` records, untracked `?` records, tab-separated rename paths, and quoted paths.
- Dirty tracked files are rejected.
- Dirty tracked prompt files are rejected, including tracked prompt files that match `.gitignore`.
- Dirty ignored prompt files are allowed only when they are not tracked.
- Explicit `--config-path` values must resolve inside the repository.
- Symlink escapes are rejected for repo input paths.

## Tests

The suite currently contains 31 tests covering:

- CLI help and placeholder exit behavior.
- Config loading, validation, and CLI override precedence.
- XDG path resolution.
- Permission behavior on chmod-capable filesystems.
- Atomic writes.
- Run ID format.
- SHA-256 helpers.
- Git repository discovery.
- Path containment.
- Symlink/config escape rejection.
- Staged-change rejection.
- Unrelated dirty/untracked file rejection.
- Modified tracked file rejection.
- Plan dirty exception.
- Prompt ignored/untracked exception.
- Tracked prompt matching `.gitignore` rejection.
- Empty stdin rejection for `prepare`.
- `prepare` artifact creation.
- Manifest hash consistency.
- Invalid logs component validation.
- Static JSON schema availability.

Permission tests use a `permission_test_root` fixture that chooses `/tmp` or another native temp directory and skips when Unix permission bits cannot be enforced. This avoids false failures on WSL drvfs paths such as `/mnt/c`.

## Validation Results

Final validation run after review fixes:

```text
git diff --cached --check                 -> passed
uv run python -m ruff format --check .    -> 29 files already formatted
uv run python -m ruff check .             -> passed
uv run python -m mypy src                 -> passed
uv run python -m pytest -q -s             -> 31 passed
uv run python -m build                    -> built sdist and wheel successfully
```

Build artifacts are ignored by `.gitignore` and should not be staged.

## Review Findings Fixed During Phase 1

The following review findings were confirmed and fixed:

- `git status --porcelain=v2` tracked-change parsing ignored space-separated records.
- Prompt source paths were allowed even when tracked.
- Explicit config paths could point outside the repository.
- `manifest.json` used a hash from the original config file while naming the persisted `source-config.yaml` artifact.
- Generated artifacts such as `dist/` and `__pycache__/` had been staged.
- `logs --component <bad>` raised a raw `ValueError`.
- `git check-ignore --no-index` allowed tracked prompt files matching `.gitignore`.
- Permission tests assumed every non-Windows filesystem honors chmod.
- Two tests needed `ruff format`.

## Current Repository Notes

During the Phase 1 review, project-local `.agents` skill files appeared in local Git status at different points:

```text
.agents/skills/create-cursor-plan/SKILL.md
.agents/skills/create-cursor-plan/agents/openai.yaml
.agents/skills/review-staged-changes/SKILL.md
.agents/skills/review-staged-changes/agents/openai.yaml
```

Those files are not part of the core Python package described above. Before committing or starting Phase 2, re-check `git status --short --untracked-files=all` and confirm whether any `.agents` assets are intentionally included.

Earlier Windows metadata file `plan-2-build-ai-dev-loop-orchestrator.md:Zone.Identifier` should still be treated as a Windows artifact and not modified unless explicitly requested.

## Phase 2 Handoff Notes

Recommended Phase 2 direction:

1. Implement `start` preflight using the prepared run state.
2. Add run and repository locks before any mutation.
3. Verify prepared hashes, branch, HEAD, repo path, plan snapshot, prompt source, and worktree baseline before invoking agents.
4. Verify local executables for `git`, `agent`, and `codex`.
5. Probe Cursor model availability and auth non-destructively.
6. Create and persist one Cursor chat ID with `agent create-chat`.
7. Implement Cursor headless execution with the Phase 0 command shape, process-group handling, raw JSONL capture, final result extraction, timeout state, and durable checkpoints.

Do not start Codex review execution until Cursor run persistence and Git staging checkpoints are reliable.

Carry forward these non-negotiable constraints:

- Do not create commits, tags, pushes, stashes, resets, or cleans.
- Do not use `shell=True`.
- Do not execute text returned by agents.
- Do not run model activity in tests.
- Do not store authentication tokens or full process environments.
- Keep all run state outside the target repository.
- Preserve enough artifacts for manual recovery.

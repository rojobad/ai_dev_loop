# Phase 1 Plan: Foundation + `prepare`

## Purpose

Build the first executable slice of `ai_dev_loop`: the Python package foundation, command surface, durable local state primitives, repository/config validation, and a fully working `ai_dev_loop prepare` command.

This phase must not implement the Cursor/Codex execution loop yet. It must create the foundation that later phases can safely extend.

## Inputs To Read First

- `plan-2-build-ai-dev-loop-orchestrator.md`
- `phase-0-findings.md`
- `.cursor/rules/ai-dev-loop-governance.mdc`
- This file

Treat the master plan as authoritative. Use this file only to narrow Phase 1 scope and incorporate Phase 0 discoveries.

## Phase 0 Constraints To Apply

- The repo is running under WSL2.
- Host Python is `python3` 3.10.12; `python`, `python3.11`, `pip`, `pipx`, `uv`, `pyenv`, and `asdf` were not present during Phase 0.
- Do not modify or replace system Python.
- Implement the package as Python 3.11+ and document `uv` as the recommended runtime/install workflow:
  - `uv python install 3.11`
  - `uv venv --python 3.11 .venv`
  - `uv sync`
  - `uv run python -m pytest`
  - `uv tool install .`
- Keep `pipx install .` documented only as an optional alternative.
- Preserve the existing `plan-2-build-ai-dev-loop-orchestrator.md:Zone.Identifier` artifact unless the user explicitly says otherwise.
- Do not stage, commit, push, tag, reset, clean, or stash.

## Deliverables

Create the standard Python package:

```text
pyproject.toml
LICENSE
README.md
scripts/development-install.sh
src/ai_dev_loop/
tests/
```

Use the repository layout from the master plan unless a small adaptation is clearly justified.

Recommended dependency stack:

- Typer for CLI.
- Pydantic v2 for typed models.
- PyYAML for YAML.
- platformdirs for XDG path discovery.
- pytest, ruff, mypy, build, types-PyYAML for development.
- hatchling or another standard PEP 517 backend.

Add an MIT license.

Create and preserve repo-local AI governance instructions:

- `.cursor/rules/ai-dev-loop-governance.mdc`

The rule must apply project-wide and encode the core guardrails from this phase: scope discipline, no Git staging/commits/pushes/resets/cleans/stashes, no system Python changes, XDG state outside target repos, no `shell=True`, no real Cursor/Codex model activity in tests, and no deletion of unrelated artifacts.

## Command Surface

Expose the console script exactly as:

```text
ai_dev_loop
```

Implement the CLI tree with useful help for all required commands:

```text
ai_dev_loop prepare
ai_dev_loop start <run-id>
ai_dev_loop resume <run-id>
ai_dev_loop status <run-id>
ai_dev_loop list
ai_dev_loop logs <run-id>
ai_dev_loop inspect <run-id>
ai_dev_loop abort <run-id>
ai_dev_loop doctor
ai_dev_loop integrations install
ai_dev_loop integrations uninstall
ai_dev_loop integrations status
ai_dev_loop config validate
```

Fully implement in this phase:

- `prepare`
- `config validate`
- basic read-only `status`, `list`, `inspect`, and `logs` for prepared run artifacts when practical
- basic non-mutating `doctor`
- basic non-mutating `integrations status`

For later-phase commands that are not implemented yet, return a clear message and non-zero exit code. Do not pretend the full loop exists.

## Core Modules

Implement these responsibilities with typed, tested code:

- `paths.py`: XDG config/state/cache path resolution and secure directory creation.
- `config.py`: project config loading, precedence, validation, effective config serialization.
- `state.py`: run state models, status enum, hashes, manifest support, atomic JSON/YAML/text writes.
- `process.py`: direct subprocess runner foundation, no `shell=True`, stdout/stderr capture, timeout shape for future phases.
- `runners/git.py`: Git discovery, status capture, path normalization, baseline safety checks.
- `redaction.py`: basic secret/environment redaction helpers.
- `errors.py`: user-facing exceptions and predictable CLI exit handling.
- `commands/prepare.py`: full prepare workflow.
- `commands/config.py`: config validation command.

Also add schemas under `src/ai_dev_loop/schemas/`:

- `project-config-v1.json`
- `run-state-v1.json`
- `codex-review-result-v1.json`

The Codex review schema can be added now as a static contract even though review execution is a later phase.

## `prepare` Scope

`prepare` must be real, not a placeholder.

Support flags equivalent to the master plan:

```text
--config-path
--project-name
--repo-path
--plan-path
--prompt-source-path
--codex-session-id
--cursor-command
--cursor-model
--cursor-output-format
--codex-command
--codex-review-model
--review-skill
--max-review-iterations
--cursor-timeout-minutes
--codex-timeout-minutes
--output
```

Behavior:

- Read the exact initial Cursor prompt from stdin.
- Reject empty stdin.
- Resolve config precedence: CLI flags, repository `ai_dev_loop.yaml`, optional user-global config, safe built-in defaults.
- Require and validate the target repository config unless an explicit valid config path is supplied.
- Canonicalize repository root, Git common dir, Git dir, branch, HEAD, plan path, prompt source path, and config path.
- Require repository file inputs to resolve inside the repository root.
- Reject symlink escapes.
- Use direct Git subprocess argument arrays for the preflight commands listed in the master plan.
- With `require_clean_worktree: true`, reject pre-existing staged changes and unrelated dirty/untracked files.
- Allow only the selected plan file and ignored prompt files as configured safe exceptions.
- Generate a run ID in the required format:

```text
<project-slug>-<UTC-basic-timestamp>-<short-random-id>
```

- Create the XDG state run directory outside the target repo.
- Persist:
  - `state.json`
  - `effective-config.yaml`
  - `source-config.yaml`
  - `manifest.json`
  - `plan/plan.md`
  - `plan/metadata.json`
  - `prompts/cursor-initial.txt`
  - `git/baseline-status.txt`
  - `logs/ai_dev_loop.log`
- Hash the approved plan, exact stdin prompt, source config, and effective config with SHA-256.
- Store relative artifact paths inside `state.json`.
- Write files containing prompts/session IDs/agent output with `0600` where supported; directories with `0700`.
- Output concise human text by default and JSON with `--output json`.
- JSON output must include:

```json
{
  "schema_version": 1,
  "status": "prepared",
  "run_id": "<run-id>",
  "project": "<project-name>",
  "start_command": "ai_dev_loop start <run-id>",
  "requires_codex_exit": true
}
```

- Make clear that the user must exit the active Codex TUI before running `start`.

## Configuration Contract

Support `ai_dev_loop.yaml` version `1` with the fields from the master plan.

Validation should cover:

- schema version
- project slug/name
- executable names
- non-empty Cursor/Codex model names where required
- supported output formats
- supported sandbox values
- positive timeouts and review iteration limits
- prompt filename template
- review skill name

Never parse YAML with regular expressions.

## Git Safety Details

Implement Git calls with direct subprocess arrays and explicit working directories.

Required preflight commands:

```text
git rev-parse --show-toplevel
git rev-parse --git-common-dir
git rev-parse --git-dir
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git status --porcelain=v2 --untracked-files=all
git diff --cached --name-only
```

Store the full baseline porcelain output. If the worktree contains ambiguous or unrelated changes, fail safely with a clear diagnostic.

## Tests

Add focused unit/integration tests for this phase:

- package import and CLI help
- XDG fallback path resolution
- secure directory/file permissions where the platform supports them
- YAML config validation and precedence
- run ID format
- atomic writes
- SHA-256 hashing
- path containment and symlink escape rejection
- Git discovery in a temporary repo
- clean worktree baseline success
- staged change rejection
- unrelated dirty/untracked file rejection
- allowed plan/prompt exception behavior
- `prepare` rejects empty stdin
- `prepare --output json` creates the expected run artifacts
- `state.json` and `manifest.json` contain expected hashes and relative artifact paths
- `config validate` success/failure
- basic command help for the full CLI tree
- static Codex review JSON schema is present and valid JSON

Use fake executables only if needed. Do not invoke real Cursor or Codex model activity in Phase 1 tests.

## Documentation

Update `README.md` for the implemented slice only:

- explain what Phase 1 implements and what is still pending
- recommend the `uv` workflow
- include editable development setup
- show `config validate`
- show `prepare` with stdin
- document that state is stored under XDG outside target repos
- state clearly that `start` and the automated loop are later-phase work

Do not claim the full Cursor/Codex loop works yet.

## Validation Commands

Prefer these commands when `uv` is available:

```bash
uv run python -m pytest
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m mypy src
uv run python -m build
uv run ai_dev_loop --help
```

If `uv` is not available, do not install or modify global tooling without explicit user approval. Report which validation commands could not be run.

## Out Of Scope

Do not implement in this phase:

- Cursor chat creation or execution.
- Codex review execution.
- Git staging after Cursor turns.
- Full `start` loop.
- `resume` recovery semantics.
- `abort` child process termination.
- global Codex skill installation.
- SessionStart hook installation.
- hook trust handling.
- commits, staging, tags, pushes, resets, cleans, or stashes.

Leave these commands present but clearly not implemented unless implementing a read-only/status subset is trivial.

## Acceptance Criteria

- `ai_dev_loop --help` works.
- The package metadata declares Python `>=3.11`.
- `LICENSE` is MIT.
- The full CLI command tree exists.
- `config validate` validates a fixture repo config.
- `prepare` accepts prompt content through stdin and rejects empty stdin.
- `prepare` persists plan, prompt, config snapshots, hashes, repo metadata, baseline Git status, manifest, and state under XDG state.
- `prepare --output json` returns a valid start command without starting the loop.
- No state files are written inside the target repository.
- Tests cover the Phase 1 behavior above.
- README reflects the actual implemented slice without overstating later phases.

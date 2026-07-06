# ai_dev_loop

Deterministic local orchestrator for Codex/Cursor development loops.

Phase 6 implements real `abort`. Phase 5's bounded stage-review-fix loop and `resume` remain unchanged.

A prepared or active run can be cancelled with `ai_dev_loop abort <run-id>`. Abort requests termination of an active Cursor or Codex child process group when durable active-process metadata clearly ties the child to the selected run, persists an abort-request marker, marks the run `aborted`, and preserves repository contents, staged changes, and existing run artifacts.

Global Codex integrations (`integrations install` / `uninstall`, SessionStart hook, global handoff skill) remain pending for Phase 7.

## What Phase 6 Implements

- Real `ai_dev_loop abort <run-id>` with process-group signaling for active Cursor/Codex children
- Durable abort-request marker at `locks/abort-request.json`
- Durable active-child metadata at `locks/active-process.json` while Cursor/Codex streaming subprocesses run
- Workflow abort observation before and after each loop action in `start` and `resume`
- User-aborted child processes end the run as `aborted`, not as generic failure or timeout
- Conservative stale-process safety: abort does not signal unrelated process groups
- `status`, `logs`, `inspect`, and README updates for abort diagnostics without sensitive leakage

## What Phase 5 Implements

- Shared workflow engine used by both `start` and `resume`
- Bounded review/fix loop driven only by schema-validated Codex JSON
- One Cursor chat per run, reused for every implementation and correction turn
- One Codex session per run, resumed for every review
- Exact forwarding of Codex-authored `cursor_fix_prompt` values from `prompts/fixes/NN.txt`
- Real `resume <run-id>` with conservative checkpoint planning

## What Is Still Pending

- Global Codex skill and SessionStart hook installation
- `integrations install` / `uninstall`

## Using Abort

Request cancellation while a run is active or waiting at a non-terminal checkpoint:

```bash
ai_dev_loop abort <run-id>
```

Abort:

- writes `locks/abort-request.json`
- signals the active child process group when metadata is clearly tied to the run
- marks the run `aborted` immediately when no workflow lock is held and no live child is running
- otherwise leaves the abort request for the active `start`/`resume` workflow to observe

Abort does **not** commit, push, reset, clean, stash, unstage, delete artifacts, or remove lock files.

Repository contents and staged changes remain exactly as they were when abort was requested. Inspect `cursor/`, `codex/`, `git/`, and `logs/events.jsonl` for the audit trail. Terminal runs (`completed`, `failed`, `aborted`, etc.) refuse abort with a clear no-op message.

## Recommended Setup (WSL)

Use [uv](https://docs.astral.sh/uv/) so Python 3.11+ is available without modifying system Python:

```bash
uv python install 3.11
uv venv --python 3.11 .venv
source .venv/bin/activate
uv sync --all-extras
```

Or run the helper script:

```bash
bash scripts/development-install.sh
```

### Editable development install

```bash
uv sync --all-extras
uv run ai_dev_loop --help
```

### User-level product install

```bash
uv tool install .
ai_dev_loop --help
```

Optional alternative: `pipx install .` if you already manage CLIs with pipx.

## Target Repository Configuration

Create `ai_dev_loop.yaml` at the repository root:

```yaml
version: 1

project:
  name: my-project

cursor:
  command: agent
  model: composer-2.5-fast
  output_format: stream-json
  force: true
  trust_workspace: true
  sandbox: disabled

codex:
  command: codex
  review_model: o4-mini
  review_skill: review-staged-cursor-execution
  sandbox: workspace-write

workflow:
  max_review_iterations: 3
  require_clean_worktree: true
  stage_mode: all
  cursor_timeout_minutes: 90
  codex_timeout_minutes: 90

prompt:
  directory: docs/plans
  filename_template: prompt_{plan_stem}.txt
```

Validate it:

```bash
ai_dev_loop config validate --repo /path/to/repo
```

## Prepare A Run

From the active Codex session, pipe the exact approved Cursor prompt on stdin:

```bash
ai_dev_loop prepare \
  --repo-path /path/to/repo \
  --plan-path docs/plans/my-plan.md \
  --prompt-source-path docs/plans/prompt_my-plan.txt \
  --codex-session-id "<exact-session-id>" \
  --output json < docs/plans/prompt_my-plan.txt
```

JSON output includes:

```json
{
  "schema_version": 1,
  "status": "prepared",
  "run_id": "my-project-20260704T134512Z-8f43c1",
  "project": "my-project",
  "start_command": "ai_dev_loop start my-project-20260704T134512Z-8f43c1",
  "requires_codex_exit": true
}
```

**Important:** exit the active Codex TUI before running `start` or `resume`.

## Start A Prepared Run

```bash
ai_dev_loop start <run-id>
```

`start` runs the full bounded loop when possible. Successful completion leaves repository changes staged. If Codex still reports actionable findings when `max_review_iterations` is reached, the run stops in `max_iterations_reached` with the latest review and fix prompt preserved.

## Resume A Checkpointed Run

```bash
ai_dev_loop resume <run-id>
```

`resume` continues from clear checkpoints without rerunning completed agent turns when durable artifacts prove they finished. Supported checkpoints include `prepared`, `waiting_for_cursor_fix`, `staging`, `reviewing`, and `interrupted`.

Inspect results:

```bash
ai_dev_loop status <run-id>
ai_dev_loop inspect <run-id>
ai_dev_loop logs <run-id>
ai_dev_loop logs <run-id> --component codex
```

## State Storage

Run artifacts are stored under XDG state, not inside the target repository:

```text
$XDG_STATE_HOME/ai_dev_loop/runs/<project-slug>/<run-id>/
├── state.json
├── effective-config.yaml
├── source-config.yaml
├── manifest.json
├── plan/
├── prompts/
│   └── fixes/
├── cursor/
│   ├── chat.json
│   └── iterations/
├── codex/
│   ├── events/
│   └── reviews/
├── git/
│   ├── status/
│   └── diffs/
└── logs/
    ├── ai_dev_loop.log
    └── events.jsonl
```

Repository locks live under:

```text
$XDG_STATE_HOME/ai_dev_loop/repository-locks/
```

Fallback when XDG variables are unset:

- config: `~/.config/ai_dev_loop`
- state: `~/.local/state/ai_dev_loop`
- cache: `~/.cache/ai_dev_loop`

## Useful Commands

```bash
ai_dev_loop doctor
ai_dev_loop integrations status
ai_dev_loop list
ai_dev_loop status <run-id>
ai_dev_loop inspect <run-id>
ai_dev_loop logs <run-id>
ai_dev_loop resume <run-id>
```

## Development Validation

```bash
uv run python -m pytest -q -s
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m mypy src
uv run python -m build
uv run ai_dev_loop --help
```

## Safety Model

- `prepare` rejects empty stdin and unrelated dirty worktrees when `require_clean_worktree: true`
- `start` and `resume` re-validate branch, HEAD, plan/prompt hashes, and prepared baseline where applicable
- Correction turns require the staged index to still match the previous orchestrator-recorded staged patch
- Codex review resumes the exact prepared session ID and decides outcomes from structured JSON, not Markdown parsing
- The orchestrator forwards Codex fix prompts exactly as stored; it does not rewrite them
- Repository file inputs must resolve inside the repository root; symlink escapes are rejected
- Subprocesses use direct argument arrays (`shell=False` is never used)
- Prompts, session IDs, staged patches, review artifacts, and agent output files are written with user-only permissions
- The orchestrator stages changes with `git add -A` for `stage_mode: all` but does not commit, push, tag, reset, clean, stash, or unstage

## License

MIT

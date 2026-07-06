# ai_dev_loop

Deterministic local orchestrator for Codex/Cursor development loops.

Phase 5 implements the complete bounded stage-review-fix loop and real `resume`. A prepared run can now move through Cursor implementation, Git staging, Codex review, optional Cursor correction turns, and additional reviews until Codex reports no actionable findings, residual-risk completion applies, or `workflow.max_review_iterations` is reached.

`ai_dev_loop resume <run-id>` continues from durable checkpoints such as `waiting_for_cursor_fix`, `staging`, `reviewing`, and `interrupted` without changing Cursor chat identity or Codex session identity.

`abort` and global integrations install/uninstall are still not implemented.

## What Phase 5 Implements

- Shared workflow engine used by both `start` and `resume`
- Bounded review/fix loop driven only by schema-validated Codex JSON
- One Cursor chat per run, reused for every implementation and correction turn
- One Codex session per run, resumed for every review
- Exact forwarding of Codex-authored `cursor_fix_prompt` values from `prompts/fixes/NN.txt`
- Multi-iteration artifacts:
  - `cursor/iterations/01`, `02`, `03`, ...
  - `git/diffs/NN.patch`, `git/status/NN-*`
  - `codex/reviews/NN.json`, `codex/events/NN.jsonl`
  - `prompts/fixes/NN.txt`
- Correction-iteration Git safety:
  - initial staging still rejects pre-existing staged paths
  - correction turns require the current staged patch to match the previous orchestrator-recorded patch
  - correction turns reject unstaged tracked changes and unexpected untracked files
- Real `resume <run-id>` with conservative checkpoint planning and partial-attempt artifact preservation
- Terminal outcomes:
  - `completed`
  - `completed_with_residual_risk`
  - `max_iterations_reached`
  - `waiting_for_cursor_fix` as a durable checkpoint between loop iterations or for manual resume

## What Earlier Phases Still Provide

- Phase 4 Codex review execution, structured result validation, and review artifacts
- Phase 3 Git staging with `git add -A` for `stage_mode: all`
- Phase 2 start preflight, locks, CLI probes, and Cursor execution
- Phase 1 `prepare`, config validation, read-only inspection, XDG storage, and secure permissions

## What Is Still Pending

- `abort` child-process termination
- Global Codex skill and SessionStart hook installation
- `integrations install` / `uninstall`

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

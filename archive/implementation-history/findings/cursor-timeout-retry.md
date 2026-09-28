# Cursor timeout retry acceptance

## Scope and decisions

Implemented in `codex/cursor-timeout-retry`, in the separate
`/home/rojobad/Projects/ai_dev_loop_timeout_retry` worktree, based on `c050031`.
The user approved same-run retries, retaining staged and unstaged work, with a
30-minute automatic delay and three automatic retries per Cursor turn. Manual
retry is available from the first timeout and can advance any pending delay.
The user explicitly removed recovery/migration of the already-blocked historical
run from scope. No historical checkpoint reconstruction or sequence reopening
is implemented.

## Implementation

- `scheduler cursor-retry <run-id>` schedules or advances one pending turn.
- Successful timeout ingestion retains the conversational state, reservation,
  exact prompt/chat/reviewer/iteration and creates a fresh attempt when due.
- Manual acceleration does not consume the pending automatic retry allocation.
- The existing SQLite transaction and effect claim serialize duplicate requests;
  active/uncertain attempts, abort requests and missing reservations reject retry.
- Timeout continuation tolerates partial index/worktree edits. Frozen prompt,
  correction artifact and repository identity verification remains enforced.
- Successful Cursor completion returns to normal staging and review, clearing
  timeout metadata. Provider usage-limit continuation preserves its own handling.
- Two optional checkpoint fields have defaults for historical readers of old
  payloads; versioned JSON schemas describe checkpoint and journal event payloads.
- No run replacement, Git rollback, re-admission or additional review-budget use.

## Validation

- `TMPDIR=/tmp TEMP=/tmp TMP=/tmp uv run pytest -q`: **1135 passed, 3 skipped**,
  29 warnings; 1163.82 seconds.
- `uv run pytest -q tests/integration/test_cursor_timeout_retry.py`: **8 passed**.
  Real scheduler, attempt runner, fake CLI processes and timeout cleanup cover
  initial/correction retries with dirty staged+unstaged work, exact prompt/chat and
  reviewer reuse, automatic exhaustion/manual continuation, concurrent manual
  requests, abort cancellation, artifact tampering, active-attempt rejection,
  three-phase sequence completion and schema/historical-checkpoint validation.
- `uv run mypy src/ai_dev_loop`: passed (146 source files).
- `uv run ruff check src tests/integration/test_cursor_timeout_retry.py`: passed.
- `uv run ruff format --check src tests`: passed (265 files).
- `git diff --check`: passed.
- `uv run mkdocs build --strict`: passed.
- `uv build`: sdist and wheel built successfully.
- Global `uv run ruff check src tests` reports three existing violations:
  I001 and UP012 in `tests/integration/test_phase21_2_run_inspection.py`, and I001
  in `tests/unit/integration_api/test_artifact_reader.py`. Reproduced on their
  unchanged HEAD contents using `git show HEAD:<path> | uv run ruff check
  --stdin-filename <path> -`. They are outside this change.

## Workstation actions and limitations

Verified that the installed scheduler list contained no active runs before
installation. Installed the isolated worktree with `uv tool install --force`,
then constrained installation to dependencies exported from the tested lockfile:

```bash
uv export --no-dev --no-emit-project --no-hashes --format requirements-txt \
  --output-file /tmp/ai-dev-loop-timeout-retry-constraints.txt
uv tool install --force --constraints /tmp/ai-dev-loop-timeout-retry-constraints.txt \
  /home/rojobad/Projects/ai_dev_loop_timeout_retry
```

Verified the installed CLI help, retry delay (1800 seconds), automatic limit (3),
and read-only access to the original run and sequence. They remain blocked at
ordinal 3. No real model calls, timer/lingering changes, source-worktree edits,
staging, commits, or historical run recovery were performed. Changes are left
unstaged in the isolated worktree for review/integration; reinstalling another
checkout without this change will remove the capability from the executable.

# Phase 20.5.1 Findings — Capacity Null-Marker Hotfix

## Parser Correction

- `_reached_marker_status` now treats explicit JSON `null` (`None` in Python) as a
  valid not-reached marker, matching the authenticated WSL App Server shape
  where `rateLimitReachedType: null` accompanies available numeric windows.
- Missing `rateLimitReachedType` behavior is unchanged.
- Nonempty strings still classify as exhausted; empty/whitespace strings,
  booleans, numbers, lists, and objects remain malformed.
- Exhaustion precedence is unchanged: a valid exhausted window or nonempty
  reached marker still wins over a sibling malformed record or marker.

## Abandoned Phase 20.6 / 20.6.5 Decision

Phase 20.6 authenticated fresh-review recovery and the follow-on Phase 20.6.5
rollover effort were **not** accepted, committed, installed, or merged into this
repository.

Repeated independent review found retained P1 defects in their proposed Git and
recovery authority boundaries, including:

- non-atomic coordination between SQLite recovery state, managed worktrees,
  private refs, and target-branch compare-and-swap integration;
- target-worktree overwrite risk when frozen source-tree invariants drift during
  recovery integration; and
- insufficiently bounded cleanup authority relative to durable recovery intent.

Those prototypes remain local audit evidence only. This hotfix does not import,
recreate, or clean up any abandoned recovery/rollover worktrees or scheduler
artifacts.

## Test Evidence

Focused Phase 20.5.1 slice:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_5_reliable_codex_limit_detection.py
```

Result: **92 passed**.

Full suite:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
```

Result: **984 passed**, 2 skipped.

Also passed before staging:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
uv build
```

# Phase 17 Central Scheduler Handoff

Date: 2026-09-09

## Summary

Phase 17.7 retired the legacy A/B and PR-review public surfaces, made the
central scheduler the only supported workflow, added explicit legacy-state
cutover cleanup with path validation and tick coordination, and finalized
packaged systemd timer install/status/disable helpers. Active documentation was
updated to the surviving scheduler contract. Automated validation passed with fake
agents only on native WSL temp paths.

**Human acceptance still required** before running destructive cleanup against a
real user-global state root or enabling a production systemd timer.

## Pre-cutover Gate

Consumed `archive/implementation-history/findings/phase-17-precutover-acceptance.md`
as the consolidated Phase 17.1–17.6 automated gate (ELIGIBLE).

## Commands Run And Outcomes

Environment: native WSL temp paths for pytest (`TMPDIR=/tmp TMP=/tmp TEMP=/tmp`).
All commands used `uv run` from the repository root. Only fake `agent` and fake
`codex` executables were used through pytest fixtures. No real state cleanup, timer
enablement, model calls, or global integration mutations were performed.

| Command | Outcome |
|---|---|
| `TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q` | **PASSED** — 548 passed, 1 skipped, 3 warnings (correction validation) |
| `uv run python -m ruff format --check .` | **PASSED** |
| `uv run python -m ruff check .` | **PASSED** |
| `uv run python -m mypy src` | **PASSED** — 95 source files |
| `uv run python -m build` | **PASSED** |
| `uv run mkdocs build --strict` | **PASSED** |
| `git diff --check` | **PASSED** |

## Retired Public Surfaces

Removed from CLI and implementation:

- legacy `prepare`, `start`, `resume`, `recover`, `extend`, `launch`, top-level
  `abort`, `status`, `list`, `logs`, `inspect`;
- entire `pr-review` command tree and `github doctor`;
- legacy engines: `workflow_engine`, `run_discovery`, `launcher`, PR-review v2
  package, and related command modules.

Preserved public workflow:

- `scheduler submit|start|tick|status|list|abort|history`
- `scheduler cutover cleanup`
- `scheduler timer validate|install|status|disable`
- `controller status`
- `doctor`, `config validate`, `integrations *`

## New Control-plane Surfaces

### Legacy cutover cleanup

```bash
ai_dev_loop scheduler cutover cleanup --confirm delete-legacy-state [--dry-run]
```

Deletes only `$XDG_STATE_HOME/ai_dev_loop/runs/` and
`$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/` after validating:

- state root symlink/ambiguity before `resolve()`;
- exact child names (no `-wal`/`-shm` siblings outside the trees);
- no symlink targets;
- no active scheduler tick lease, attempt, or capacity holder;
- file lock held for the full deletion interval plus tick lease coordination;
  ticks honor the cutover advisory lock even when the SQLite lease TTL elapses;
- explicit confirmation token.

JSON mode emits exactly one JSON document on stdout; path announcements go to stderr.

### Scheduler timer operations

```bash
ai_dev_loop scheduler timer validate
ai_dev_loop scheduler timer install [--enable]
ai_dev_loop scheduler timer status
ai_dev_loop scheduler timer disable
```

Install writes owned user units under `~/.config/systemd/user/` and reloads
`systemctl --user`. Enable is explicit via `--enable`; normal `submit`/`tick` do
not auto-enable timers.

## Automated Test Coverage Added

- `tests/unit/scheduler/test_phase17_7_cutover.py` — confirmation, exact roots,
  internal WAL/SHM removal with sibling preservation, symlink refusal (root,
  child, and XDG ancestor), outside data preserved on refused paths, active tick
  lease block, competing tick refusal while cutover holds lease (with and without
  pre-existing `engine.sqlite3`), behavioral regression when simulated time advances
  beyond the persisted lease `expires_at` during a long first deletion (with
  temporary guard disable proving SQLite lease expiry alone would allow takeover),
  two-root ledger binding,
  concurrent cleanup refusal, dry-run, idempotency.
- `tests/unit/scheduler/test_phase17_7_cutover_cli.py` — JSON stdout-only and text
  announcement modes.
- `tests/unit/scheduler/test_phase17_7_timer_ops.py` — owned install, enable,
  non-owned refusal, status with injected fake runner, disable (fake `systemctl`).

Scheduler acceptance for fresh-B bootstrap and exact resume remains in
`tests/integration/test_phase17_5_scheduler_review_loop.py` and related unit
tests.

## Documentation Cutover (Phase 17.7)

Updated active docs to scheduler-only contract:

- `docs/index.md`, `docs/guia/guia-rapida.md`, `docs/guia/flujo-handoff.md`,
  `docs/guia/flujo-completo.md`
- `docs/operacion/prepare-start-resume-abort.md`, `observabilidad.md`,
  `estado-artefactos.md`, `seguridad-privacidad.md`, `desinstalacion-limpieza.md`
- `docs/referencia/cli.md`, `configuracion.md`

Archived history under `archive/implementation-history/` is preserved unchanged.

## Unperformed Manual Actions

Intentionally not executed in this phase:

- Real Cursor or Codex model calls
- Real `systemctl --user` timer install/enable/observe/disable on a production
  workstation
- Destructive cleanup of a real user-global `$XDG_STATE_HOME/ai_dev_loop/runs/`
  or `pr-review-v2/` tree
- Changes to global integrations (`~/.agents`, `~/.codex`, hooks, bridge)

## Manual WSL Acceptance (Required Before Production Use)

Use a **disposable XDG state root** isolated from production (`XDG_STATE_HOME`
pointing to a temporary directory). Use only **fake `agent` and fake `codex`**
executables on `PATH` (as in automated tests). Do not run against a real user-global
state tree or enable a production timer without independent human authorization.

Before enabling a timer or deleting legacy state on a real machine:

1. Independent human acceptance review of control-plane and destructive changes.
2. In the disposable state root, run fake-agent `scheduler submit` / `scheduler start`
   / `scheduler tick` through review and correction boundaries; confirm one fresh B
   bootstrap and exact later resume (no `--last`, no second B).
3. Run `scheduler timer install` and `scheduler timer status` with fake or disposable
   units; observe at least one fake tick cycle; then `scheduler timer disable`.
   **Timer enablement (`install --enable`) is a separate explicit authorization**
   and must not be combined with the first acceptance pass unless deliberately approved.
4. Run `scheduler cutover cleanup --dry-run --confirm delete-legacy-state`, inspect
   stderr path announcements and JSON/text output; run the real command only after
   explicit authorization on the exact resolved paths shown.

## Residual Risks

- Production timer behavior and WSL-active-only progress remain unverified outside
  fake `systemctl` tests.
- Real destructive cleanup has not been executed against user-global state.

## Next Safe Action

1. Independent human acceptance review of control-plane and destructive changes.
2. Manual WSL fake-agent acceptance on a disposable state root.
3. Only then: optional `scheduler cutover cleanup --confirm delete-legacy-state`
   and `scheduler timer install --enable` on the production workstation.

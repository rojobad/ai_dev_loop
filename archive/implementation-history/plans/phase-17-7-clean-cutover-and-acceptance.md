# Phase 17.7 — Clean cutover and acceptance

## Goal

Make the central scheduler the only supported product workflow after all prior
Phase 17 behavior is proven. Retire the legacy A/B and PR-review surfaces, and
perform the user-authorized cleanup of exactly the two legacy state roots only
through an explicit, verified cutover operation.

## Non-Goals

- Do not migrate, parse, preserve, or resume any old run.
- Do not delete config, cache, `codex-sessions`, hooks, installed skills, target
  repository data, or arbitrary XDG paths.
- Do not add PR-review/GitHub behavior or Windows WSL wake-up.

## Scope

- Remove public legacy run and PR-review commands/docs/tests once equivalent
  scheduler behavior has passed Phases 17.1–17.6.
- Add an explicit destructive cutover cleanup limited to
  `$XDG_STATE_HOME/ai_dev_loop/runs/` and
  `$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/`, including their SQLite `-wal`/
  `-shm` sidecars and artifacts.
- Finalize systemd timer install/enable documentation and perform separately
  authorized manual WSL acceptance with fake agents.
- Produce the Phase 17 findings handoff and final active documentation.

## Out of Scope

- Silent cleanup during package import, `scheduler submit`, `start`, `tick`, or
  test setup.
- Recursive deletion based on unvalidated environment variables, symlinks,
  globs, the XDG root itself, the home directory, or target repository paths.
- Real model, GitHub, commit, push, or destructive repository acceptance tests.

## Required Context

Read the master, the consolidated pre-cutover findings artifact
`archive/implementation-history/findings/phase-17-precutover-acceptance.md`,
and any available results/findings for Phases 17.1–17.6; all Cursor rules; `AGENTS.md`;
`cli.py`, path/config/integration docs, current cleanup/locking primitives,
`README.md`, all relevant `docs/`, and the staged-review skill. Inspect the
actual XDG layout read-only before defining cleanup tests or code. An absent,
incomplete, or failed pre-cutover artifact blocks cutover; the artifact is not
authority to delete user state or enable a timer.

## Cursor Rules And Skills

All `.cursor/rules/*.mdc` apply. This phase changes control-plane and destructive
surfaces: follow `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`
and require a human acceptance review after automated tests. The staged-review
skill must review the final staged diff independently.

## Architecture Guardrails

- Final authority is only `engine.sqlite3` plus `artifacts/`; no legacy state
  reader, PR-review engine, or hidden compatibility routing may remain public.
- Cleanup resolves `state_dir()` and accepts only the exact literal child names
  `runs` and `pr-review-v2`. Reject nonexistent ambiguous/symlink/non-directory
  targets; never traverse outside the resolved state root.
- Require an explicit destructive CLI confirmation token and show the two exact
  resolved paths before deletion. Do all validation before any mutation. Report
  deletion outcome and irrecoverability; never delete preserved paths.
- Tests use temporary XDG roots. Manual acceptance must use fake agents and a
  reviewed disposable state root before any user-global deletion/enablement.
- Documentation must state actual supported command names, state location,
  systemd setup, controller observability, one fresh Codex B per run with
  exact later resumes, recovery boundaries, and WSL limits.

## Implementation Plan

1. Audit the consolidated pre-cutover findings artifact and any available
   Phase 17.1–17.6 findings. Require a passing full validation record and an
   explicit phase-to-evidence matrix; missing, incomplete, or failed evidence
   blocks cutover. Do not cut over based on code presence alone.
2. Remove legacy local A/B fork/pre-existing-reviewer public CLI registration,
   configuration references, docs claims, package assets, and tests that only
   assert retired behavior. Retain shared primitives only where the scheduler
   demonstrably uses them; remove dead compatibility code rather than leaving a
   second authority. Preserve the scheduler's model/reasoning-at-submit and
   one-fresh-B-then-exact-resume contract in the surviving docs and assets.
3. Add a narrowly named explicit cutover cleanup command. Validate state root,
   exact child paths, symlink status, confirmation token, and no active scheduler
   attempts/leader before removing the two approved roots and sidecars. Prefer a
   recoverable staging/trash mechanism if it can preserve the exact scope; if
   permanent removal is selected, state it clearly in CLI/docs.
4. Finalize systemd user service/timer install/status/disable commands or manual
   instructions with ownership/idempotency checks. No command may enable it as a
   side effect of normal scheduler use.
5. Update all active docs and create
   `archive/implementation-history/findings/phase-17-central-scheduler-handoff.md`
   with automated results, manual WSL fake-agent result, remaining risks, and
   exact cleanup outcome.
6. Request independent staged review and human acceptance for all control-plane
   and destructive changes before any real state cleanup or timer enablement.

## Testing Criteria

- Full suite after legacy test removal/update; targeted CLI help/config/package
  checks prove no retired command is advertised and scheduler commands remain.
- Temporary-XDG cleanup tests cover confirmation required, exact valid roots,
  WAL/SHM/artifacts, no active-work deletion, nonexistent paths, symlink/refusal,
  traversal attempts, preserved config/cache/session/hook/skill paths, and
  repeated cleanup idempotency.
- Package asset tests cover timer/service content and explicit install/disable
  behavior without calling the real user manager.
- Fake scheduler acceptance verifies the first review creates one read-only B
  from submit-frozen model/reasoning and later corrections resume it; it must
  never fork, use `--last`, or create a second B.
- Manual WSL acceptance: enable timer only after human review, observe a fake
  agent lifecycle and recovery across ticks, then disable it; do not use models.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m build
uv run mkdocs build --strict
git diff --check
```

## Risks Or Recovery Notes

This phase is destructive by design and is the only phase authorized to remove
old state. The user's permission does not authorize broad deletion: an
unresolved/symlinked target, live scheduler attempt, or missing confirmation must
leave all data untouched. Automated evidence does not replace human review of
timer installation, cleanup scope, and final documentation.

## OpenQuestions

None. The user explicitly authorized deletion only for the two exact legacy XDG
roots, after the phased implementation is complete.

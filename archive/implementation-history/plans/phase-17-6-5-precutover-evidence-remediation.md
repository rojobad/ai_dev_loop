# Phase 17.6.5 — Pre-cutover evidence remediation

## Goal

Repair the committed Phase 17.6 formatting defect and create a factual,
reproducible pre-cutover acceptance record for the current Phase 17 baseline.
That record is the evidence gate Phase 17.7 must audit before it can retire a
legacy surface, enable a timer, or offer the destructive cleanup command.

## Non-Goals

- Do not implement any Phase 17.7 cleanup, legacy-command removal, timer
  enablement, service installation, or user-global integration change.
- Do not execute a real model, a real systemd user service, GitHub action, or
  destructive state cleanup.
- Do not reinterpret historical runs, claim a manual acceptance occurred, or
  fabricate passing evidence from a commit or a prior chat summary.

## Scope

- Apply the mechanical formatter result to
  `src/ai_dev_loop/scheduler/application/status.py` and verify that it remains
  formatting-only.
- Run the full automated validation suite on the committed baseline using fake
  agents only.
- Create `archive/implementation-history/findings/phase-17-precutover-acceptance.md`.
  It must record the exact baseline revision, commands actually run, concise
  pass/fail outcomes, phase-to-test evidence mapping for Phases 17.1–17.6,
  unperformed manual actions, residual risks, and the exact Phase 17.7 gate.
- Update the Phase 17.7 plan so its audit step explicitly requires that
  consolidated findings artifact. The artifact must state that an incomplete or
  failed validation blocks cutover; it is not authority to clean user state or
  enable a timer.
- Approved test-remediation exception: correct only the three failures found by
  the initial full-suite run. Add `timed_out = False` to the two fake process
  results in `tests/unit/test_probes.py`; in
  `tests/integration/test_phase16_4_acceptance_matrix.py`, assert deferred
  reply dispatch before completing the queued replies and asserting operator
  continuation. These are regression-test repairs only and must not change
  probe, reducer, scheduler, schema, CLI, or persistence behavior.

## Out of Scope

- `ai_dev_loop.yaml`, global skills, hooks, bridge assets, target repositories,
  XDG state, and legacy run artifacts.
- Any production command that deletes a state root, enables/disables systemd,
  stages, commits, pushes, resets, cleans, stashes, or unstages Git content.
- Changes to scheduler behavior, schemas, CLI contracts, or test expectations
  beyond the named, test-only remediation exception above.

## Required Context

Read `AGENTS.md`, every `.cursor/rules/*.mdc`, the archived master plan,
Phases 17.1 through 17.7, the existing findings directory, the Phase 17.6
commit and tests, and the current CLI/docs before writing acceptance claims.
Read `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md` and do
not treat the absence of a historical findings file as evidence that a manual
acceptance occurred.

## Cursor Rules And Skills

All `.cursor/rules/*.mdc` and `AGENTS.md` apply. In particular, follow the
documentation/acceptance, governance, privacy, global-integration, abort,
state, and loop/recovery contracts. Use
`.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md` for the
findings artifact. Do not invoke the staged-review skill during implementation;
it remains an independent post-implementation review.

## Architecture Guardrails

- The evidence artifact is an audit record, not a replacement scheduler state,
  manifest, test result, or authorization for destructive work.
- Derive every documented result from an observed command, source file, test,
  CLI help, or prior findings artifact. State unknown or unperformed items
  explicitly; do not infer them from code presence.
- Automated validation uses fake `agent` and fake `codex` executables only.
  Never use `--last`, real model calls, real timer enablement, or real cleanup.
- Default documentation and findings must not expose prompts, patches, review
  bodies, raw JSONL, full session IDs, process environments, or authentication
  material.
- A validation failure leaves the evidence artifact explicitly blocked and
  leaves Phase 17.7 ineligible. It must not be hidden by formatting unrelated
  files or broadening scope.
- Preserve the Phase 17.7 rule that human acceptance is required before any
  real timer enablement or cleanup operation.

## Implementation Plan

1. Confirm the worktree starts clean and capture the current commit identifier
   for the findings record. Inspect `ruff format --diff` for the designated
   status module. If the proposed change is not formatter-only, stop and report
   the broader change instead of making it.
2. Apply only the formatter's mechanical edit to the designated module. If the
   initial full-suite run exposes exactly the three named stale test failures,
   apply the approved test-only remediation exception. Do not otherwise change
   behavior, tests, or public output while repairing this prerequisite.
3. Inventory existing Phase 17 tests, plans, committed implementation surfaces,
   and findings. Build a phase-to-evidence matrix from this inventory; mark
   missing historical manual evidence as unperformed rather than recreating it.
4. Run the full validation command set below with native WSL temporary paths.
   Capture only safe command summaries, aggregate outcomes, and references to
   committed tests in the findings artifact. If any command fails, record the
   exact failing command and stop before updating the cutover gate as passing.
5. Write the consolidated pre-cutover findings artifact with: baseline revision;
   commands and outcomes; Phase 17.1–17.6 test/evidence matrix; fake-agent
   limitation; manual WSL/timer/cleanup status; residual risks; and the next
   safe action.
6. Amend Phase 17.7's Required Context and first implementation step to require
   that artifact as the consolidated pre-cutover evidence gate. Preserve its
   fail-closed wording, its human-acceptance requirement, and all destructive
   scope limits.

## Testing Criteria

- `ruff format --check .` passes after the mechanical repair; the diff shows no
  semantic source change in the formatted module.
- The named probe doubles expose `timed_out = False`; the acceptance matrix
  proves that queued deferred replies require `EXECUTE_EFFECT` before
  `WAIT_FOR_USER`, without modifying production behavior.
- The complete pytest suite passes using only fake agents. The findings record
  maps each Phase 17 contract area to actual current tests or labels it missing.
- Ruff, mypy, package build, MkDocs strict build, and `git diff --check` pass.
- Review the findings text for unsupported claims and sensitive data. It must
  state that no real model, real timer lifecycle, or state cleanup was run.
- The Phase 17.7 plan refuses cutover when the consolidated findings artifact is
  absent, incomplete, or reports a failed prerequisite.

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

This is a documentation and validation gate, not a cutover. A clean current
suite can prove the present baseline but cannot retroactively prove a manual
workstation action. Preserve that distinction in the findings artifact and
leave any real timer enablement or cleanup to Phase 17.7 after independent
review and explicit human authorization.

## OpenQuestions

None. The user authorized the bounded formatter and test-remediation work plus
collection of automated evidence only; real cleanup and timer enablement remain
explicitly deferred.

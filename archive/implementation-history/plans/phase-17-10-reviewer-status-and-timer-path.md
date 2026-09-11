# Phase 17.10: Reviewer Status Projection and Timer CLI Path

## Goal

Correct two independent operational defects found during the real Phase 17
acceptance run, without changing that run or the review workflow:

1. Scheduler status must display a redacted reviewer-session prefix after a
   fresh B reviewer is bound.
2. The installed user-systemd timer must be able to locate the CLI installed by
   uv tool in the ordinary user-local bin directory.

The result must preserve exact reviewer identity internally, preserve
user-controlled timer enablement, and make the installed timer portable across
users without embedding a machine-specific home directory.

## Non-Goals

- Do not mutate, resume, abort, clean up, or otherwise operate on any existing
  scheduler run, including the active Parish360 acceptance run.
- Do not change reviewer creation, binding, resume, authentication, model
  selection, prompt contents, review decisions, or state-machine transitions.
- Do not run a real Cursor or Codex model, install or enable systemd units, or
  invoke a live scheduler tick as part of this phase.
- Do not add a timer daemon, background process, shell wrapper, migration, or
  automatic timer enablement.
- Do not modify global Codex integrations, desktop/WSL bridge configuration,
  project-local YAML, or the Parish360 repository.
- Do not stage, commit, push, reset, clean, stash, rebase, or alter unrelated
  working-tree changes.

## Scope

- The scheduler status/list projection and shared controller-read projection
  that report the B reviewer identifier.
- User-systemd service asset generation and its static asset validation.
- Unit and integration tests for both regressions.
- Operational/reference documentation and a Phase 17.10 findings artifact.

## Out of Scope

- Repairing historical status records, changing the stored event ledger, or
  exposing any full session identifier.
- General PATH discovery or support for arbitrary CLI installation locations.
- Automatic retry/recovery changes, attempt timeouts, output capture, capacity
  rules, repository reservations, submit idempotency, or abort semantics.
- A real-model/systemd smoke test. That remains an explicit operator acceptance
  action after independent review, approval, commit, and local installation.

## Required Context

Read these before editing:

- AGENTS.md.
- .cursor/rules/01-project-overview.mdc.
- .cursor/rules/02-architecture.mdc.
- .cursor/rules/06-testing.mdc.
- .cursor/rules/10-safety.mdc.
- .cursor/rules/11-documentation.mdc.
- .cursor/rules/18-global-integrations.mdc.
- .cursor/skills/ai-dev-loop-governance/SKILL.md.
- src/ai_dev_loop/scheduler/application/contracts.py.
- src/ai_dev_loop/scheduler/application/status.py.
- src/ai_dev_loop/scheduler/application/controller_read.py.
- src/ai_dev_loop/scheduler/domain/state.py.
- src/ai_dev_loop/scheduler/application/attempt_service.py.
- src/ai_dev_loop/scheduler/application/codex_workflow_service.py.
- src/ai_dev_loop/scheduler/assets/ai-dev-loop-scheduler-tick.service.
- src/ai_dev_loop/scheduler/assets/ai-dev-loop-scheduler-tick.timer.
- src/ai_dev_loop/scheduler/infrastructure/systemd_assets.py.
- src/ai_dev_loop/scheduler/application/timer_ops.py.
- tests/unit/scheduler/test_phase17_7_timer_ops.py.
- tests/integration/test_phase17_1_submit.py and the scheduler status/list
  tests that already establish projection conventions.
- docs/referencia/cli.md.
- docs/operacion/prepare-start-resume-abort.md.
- docs/operacion/troubleshooting.md.
- docs/referencia/codex-desktop-wsl-sessions.md.
- archive/implementation-history/plans/phase-17-7-clean-cutover-and-acceptance.md.
- archive/implementation-history/findings/phase-17-8-codex-attempt-bounds.md.
- archive/implementation-history/findings/phase-17-9-findings.md.

The acceptance evidence to preserve is:

- A fresh B reviewer is correctly created and bound to mutable workflow state;
  later resume attempts use that bound identity.
- Status currently reads only immutable review context for its reviewer prefix.
  Fresh-review context intentionally contains no session ID, so the rendered
  reviewer prefix remains null even after binding.
- The installed timer service currently executes a bare CLI name. A uv-tool
  installation places that CLI in the user-local bin directory, which systemd
  does not necessarily inherit; the observed result is systemd exit 203/EXEC.
- The timer was disabled after diagnosis. Do not change that fact in this
  phase.

## Cursor Rules And Skills

- Use repository-local Cursor rules and skills as instructed above.
- Use fake executables, temporary paths, and injected runners only. No real
  model, real user-systemd command, live scheduler run, or external service.
- Keep changes focused. Do not refactor unrelated scheduler behavior.
- Treat full controller/reviewer IDs, prompts, artifact contents, account
  details, and local paths as private. Test only redacted prefixes where
  identity output is needed.
- Do not make Git state changes. Leave implementation changes unstaged for
  independent review.

## Architecture Guardrails

### Reviewer status projection

- The authoritative bound B identity is the mutable workflow checkpoint,
  specifically the reviewer session ID established by the authenticated binding
  event. Do not derive a new identity, duplicate it, or alter its lifecycle.
- Status is read-only. It may consume the current validated state solely to
  render an already-approved redacted prefix.
- Before a fresh B reviewer has been bound, its prefix must remain null. Legacy
  configurations that specify a reviewer session in immutable context must
  continue to render their existing redacted prefix.
- Never place the complete reviewer ID in public JSON/text status, logs, test
  snapshots, docs, exceptions, or artifacts. Reuse the established redaction
  helper rather than adding another redaction format.
- Keep all current safe-next-action and block-reason logic unchanged.

### User-systemd service asset

- The packaged service asset must remain static and user-independent. It must
  not contain a literal home directory, WSL mount path, repository path, or
  machine-specific executable path.
- Make command lookup deterministic for the supported uv-tool installation:
  run the CLI through the system env executable and set a deliberately bounded
  PATH containing the system directories plus the user-local bin directory via
  the systemd home specifier. Do not invoke a shell or source profile files.
- Retain a one-shot service and the existing 30-second timer cadence.
- Installation must remain explicit. Installing must not enable or start the
  timer unless the operator passes the existing enable option.
- Extend static asset validation so it enforces the intended executable and
  PATH contract, while retaining the current secret/path safety checks.

## Implementation Plan

### 1. Trace and define the status projection boundary

Trace every call site of summary_from_context and the controller-read/status
paths. Identify the smallest typed input needed to render the current reviewer
prefix from validated state without broadening public state serialization.

Implement a single shared projection rule:

- If immutable context carries a legacy reviewer session ID, render its existing
  redacted prefix.
- Otherwise, if current validated state has a bound reviewer session ID, render
  that ID through the existing redaction helper.
- Otherwise render null.

Do not alter fresh reviewer binding, resume selection, event data, or any
workflow state transition. Update every relevant status/list/controller-read
caller so they cannot disagree about the visible prefix. If a controller-read
surface does not currently expose this field, do not add a new public field
without a demonstrated existing status contract; instead share the same
internal projection input where it already uses the summary model.

### 2. Repair the static service command lookup

Update the scheduler tick service asset to use a shell-free system executable
to resolve ai_dev_loop under an explicit constrained PATH. The path must include
the user-local bin directory using the systemd home specifier and the normal
system binary directories. It must work when systemd starts with a minimal
environment and must not depend on CODEX_HOME.

Update the asset validator to reject a service that lacks this exact safety
contract. Keep the validation static: no host PATH probing and no test that
depends on the machine where tests run. Confirm timer operation behavior still
installs, validates, reports, disables, and uninstalls the package-owned units
with the existing ownership protections.

### 3. Add focused regressions

Add or extend tests that prove:

- A fresh-review run renders null before authenticated binding and a redacted,
  non-null B reviewer prefix after binding.
- The full B session ID is absent from each public status/list output exercised
  by the test.
- A legacy context-backed reviewer prefix remains compatible.
- The projected prefix comes from current validated state and does not mutate
  the state or change the next safe action.
- The service template includes the shell-free deterministic PATH/command
  contract and passes static validation.
- Malformed/unsafe variants that omit the contract are rejected by asset
  validation.
- Timer operations are exercised only through existing fake command runners
  and temporary unit directories; no user systemd is touched.

Use the existing fake CLI fixtures and test factories. Do not introduce
environment-variable behavior into production code merely to make tests pass.

### 4. Document operations and findings

Update documentation to explain:

- Status shows a redacted B reviewer prefix once a fresh reviewer has been
  authenticated and bound; null before then is expected.
- The scheduler timer service supports the normal uv-tool installation by
  setting its own constrained PATH; it neither reads shell startup files nor
  inherits arbitrary interactive shell paths.
- The practical operator flow for a future live acceptance: validate, install
  with enable only when explicitly desired, observe at least two timer
  activations and the run history/status progressing without manual ticks, then
  disable the timer. Disabling prevents later ticks but does not abort an
  already-started attempt.

Create archive/implementation-history/findings/phase-17-10-reviewer-status-and-timer-path.md.
Record the two observed defects, the bounded repair, validation evidence,
non-actions, residual risk, and the required human acceptance. Do not include
full session IDs, prompts, private paths, or raw protected artifacts.

## Testing Criteria

The implementation is acceptable only if all of the following are true:

- Fresh B reviewer status becomes redacted/non-null only after binding, while
  legacy behavior and unbound fresh behavior remain correct.
- No tested public output contains the full reviewer session ID.
- The repair does not change Codex invocation, review workflow, run state,
  block handling, or safe-next-action behavior.
- A static package asset provides shell-free lookup of the uv-tool CLI from a
  minimal systemd environment without a literal home or repository path.
- The asset validator rejects incomplete/unsafe service variants.
- Timer unit tests do not invoke real systemd and no test invokes real models.
- Documentation precisely describes the bounded behavior and operator-only live
  acceptance.

## Validation

Run the relevant focused scheduler status/list/controller-read and timer tests,
then run:

    TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
    uv run ruff format --check .
    uv run ruff check .
    uv run mypy src
    uv run python -m build
    uv run mkdocs build --strict
    git diff --check

Report exact outcomes and any skip count in the findings artifact. If a command
fails, stop at that validation gate, preserve evidence, and do not claim the
phase is complete.

## Risks Or Recovery Notes

- The active acceptance run must continue using manual scheduler ticks until
  this work has been independently reviewed, explicitly approved, committed,
  installed locally, and the operator deliberately enables a new timer test.
- Replacing the package-owned unit after approval is recoverable: disable the
  timer before a rollback or uninstall, and use only the existing timer CLI.
  Do not manually delete unrelated user-systemd files.
- Status display is diagnostic only; the runtime continues to use the stored
  exact reviewer identity. A bad projection must never cause a new reviewer to
  be created or a resume attempt to target another reviewer.
- A constrained service PATH intentionally supports the documented uv-tool
  layout rather than arbitrary launchers. Record any future unsupported layout
  as a separate compatibility decision.

## OpenQuestions

None. The supported CLI location and the user-systemd failure mode are known,
and the active run must remain untouched.

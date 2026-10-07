# Phase 24.1 — Timeout configuration and immutable policy contracts

## Goals

Introduce explicit activity-timeout inputs and immutable typed policy bindings
without changing the meaning of existing fixed timeouts. Deliver a safely bounded
configuration-only slice; activity execution is enabled in 24.2.

## Non-Goals

Implementing activity detection, extending process lifetime, changing retry
authority, or enabling adaptive defaults across existing configurations.

## Scope

Configuration/overrides, submit and sequence-prepare flags, manifest policy
overrides, domain/schema contracts, immutable serialization and compatibility,
admission capability guard, directly affected docs and tests.

## Out of Scope

No activity runner or retry-state implementation; no live runs, installation,
timer changes, Git side effects, root `ai_dev_loop.yaml` edits, reviewer/planning
skill edits, or legacy-command restoration. Do not change unrelated budgets.

## Required Context

Read [the Phase 24 overview](phase-24-cursor-activity-timeouts.md), `AGENTS.md`,
`docs/referencia/cli.md`, `docs/referencia/configuracion.md`, and current
state/privacy docs. Recheck `config.py`, `cli.py`, scheduler domain
`state.py`/`sequence.py`, `application/submission.py`, `sequence_prepare.py`,
`sequence_materializer.py`, `attempt_service.py`, `run_state_bridge.py`,
`infrastructure/sqlite_store.py`, `response_schema.py` and protected
artifact/state loaders.
Trace recovery context copying in `cursor_initial_recovery.py`,
`cursor_correction_recovery.py`, `sequence_cursor_recovery.py` and their actual
evidence readers. Confirm the baseline's fixed policy and sequence freeze are
implemented; archived plans alone do not establish acceptance.

Current `WorkflowLimits` has only `cursor_timeout_minutes`; manifests and frozen
entries are v1 and submitted context readers support versions 1–4. Current
systemd budgets use that fixed timeout plus grace. Read existing configuration,
submission, sequence preparation/materialization, schema/historical, attempt
executor and Phase 23 recovery tests before extending contracts.

## Cursor Rules And Skills

Follow `AGENTS.md` and the eight `.cursor/rules/` files listed in the overview:
governance, orchestrator, state/schema, loop/recovery, abort, Codex review,
docs/acceptance and global integrations. Read
`.agents/skills/create-cursor-plan/SKILL.md` and respect the separate read-only
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`. No `.cursor/skills/`
exists. Keep these skills and root YAML unchanged; update only directly affected
rule statements if necessary.

## Architecture Guardrails

- Follow the overview's exact input, precedence, null and strict-number rules.
  Preserve fixed-policy behavior, old schemas and authentic historical bytes.
- Keep configuration resolution in config/application layers, policy invariants
  in typed domain models, and CLI presentation separate from serialization.
- Introduce versioned shapes only for changed boundaries. Align models, JSON
  schemas, validators, writers/readers, hashes and copying in this same slice.
  Do not add SQLite migrations unless a changed durable table actually needs one.
- Frozen context/effective-config artifacts, not ambient YAML, determine policy.
  Read-only inspection must not migrate or normalize historical records on disk.
- No continuous Git baseline checks, real CLIs/model calls, service setup or
  secrets/transcripts in fixtures and public output.

## Implementation Plan

**A-01 — Complete explicit inputs and deterministic precedence.** Add
`workflow.cursor_activity_timeout` and the three flags specified in the overview
to submit and sequence prepare. Resolve full policy objects by the stated layer
precedence; reject partial CLI triplets and same-layer conflicts. Keep legacy
override behavior, create-chat/preflight bounds and defaults intact. Require
`stream-json` for activity turns before agent work. Add matching per-phase
manifest overrides, effective configuration and concise help/documentation.

**A-02 — Canonical immutable freeze and historical reads.** For new activity
inputs, persist an explicit versioned policy discriminator and all integer
thresholds in submitted/frozen-entry contracts and effective config. Define and
test the minimal new schema versions needed across affected context, manifest,
sequence and state envelopes; keep existing versions readable under their old
semantics. Fixed inputs may retain existing writer shapes. A historical missing
policy means fixed; do not add defaults to authenticated bytes or rewrite stored
hashes. A new activity binding cannot omit/null its thresholds or policy version.
Preserve policy when lazy materialization and existing recovery context copying
occur; updates to YAML/defaults after freeze must have no effect. Document the
canonical shape and loader dispatch actually implemented in findings.

**A-03 — Honest phase boundary and safe admission.** Until 24.2 exists,
activity-policy submissions may freeze, but their first admission/preflight
must reject unsupported execution before creating a Cursor chat or launching
an implementation agent. Check capability at admission, not continuously.
Expose a safe message identifying pending activity-runner support; do not silently
fall back to fixed execution. Existing fixed submissions execute normally.
24.2 explicitly replaces this temporary guard with real supported execution.

Changed persistence boundary: valid input -> prepare/submit freezes authenticated
policy and artifacts -> queued/prepared definition, with no Git/agents or
reservation at preparation. Replays reuse authenticated identical inputs;
changed thresholds change submission identity. Crash/replay uses the existing
protected-store publication mechanism, preserving historical ownership. Admission
of an unsupported activity policy stops safely; it grants no retry or installation
authority. Existing cancellation handling remains in force.

## Testing Criteria

Mandatory unit/schema and production CLI/service tests. Add
`tests/unit/scheduler/test_phase24_1_timeout_policy.py` and
`tests/integration/test_phase24_1_timeout_policy.py`; extend affected existing
config/schema/submission/sequence tests. Capture genuine pre-24 fixed
config/context/manifest/frozen-entry/invocation fixtures with baseline provenance
under `tests/fixtures/phase24_historical/`, without sensitive session contents.

| Contract | Independent acceptance evidence |
| --- | --- |
| A-01 | Real config loader and CLI submit/prepare accept the complete 120/20/360 input. Test omitted/null whole block, rejected malformed objects/numbers, hard/soft/window relationships, complete flag triplets, legacy overrides and conflicts at each supported layer. Adaptive non-stream output fails before agent work. |
| A-02 | Actual submit/prepare -> reload -> materialization retains exact policy; later YAML changes cannot alter it. Same inputs replay; changed policy changes identity. Genuine old fixtures load and retain their fixed serialization/hashes. New policy required/null invariants and copied recovery contexts validate. |
| A-03 | Actual start/tick admission with a fake backend proves an unsupported activity definition launches zero agent attempts, including create-chat. The same production flow still completes a fixed-policy baseline with fake agents. Help/docs accurately describe this intermediate slice. |

Use isolated native-WSL HOME/XDG/temp repos and fake external executables. Do not
make tests repair production freeze bindings or inject the admission guard.

## Validation

Run mandatory new tests and these affected selections; add concrete node IDs
only when a changed caller needs coverage. Do not run repository-wide pytest.

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase24_1_timeout_policy.py tests/integration/test_phase24_1_timeout_policy.py tests/unit/test_config.py tests/unit/scheduler/test_schema.py tests/unit/scheduler/test_schema_readonly_historical.py tests/unit/scheduler/test_phase20_1_sequence_prepare.py tests/unit/scheduler/test_phase20_2_sequence_start.py tests/integration/test_phase20_1_sequence_prepare.py
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Report A-01–A-03 production callers/tests, schema versions and exact executed,
failed and unexecuted results in
`archive/implementation-history/findings/phase-24-1-timeout-policy-contracts-findings.md`.
After correction rerun failed/affected selections. The repository-wide pipeline
gate belongs to 24.3; pending gate is distinct from missing mandatory coverage.

## Risks Or Recovery Notes

Persisted defaults can accidentally reinterpret old hashes or turn an old fixed
timeout into a soft threshold. Test immutable old inputs independently. This
intermediate checkpoint intentionally does not advertise runnable activity
support. Do not retrofit these fields into the already-frozen execution sequence.

## OpenQuestions

None. 24.2 is required before running activity-policy agent work.

# Phase 23 — Manual recovery of a failed Cursor turn

## Goals

Implement one bounded feature run: an operator can explicitly recover an eligible
`blocked(cursor_failure)` run through
`ai_dev_loop scheduler cursor-retry RUN_ID --force --output json`. Authorize one
successor to resume the exact Cursor chat and failed turn, preserving partial work.
Append a fixed operational note to the exact failed prompt. When the run belongs
to a sequence, recover that ordinal in the original sequence and continue through
the ordinary review, checkpoint, and subsequent-phase flow.

The operator approved manual same-chat recovery, including initial and correction
turns, on 2026-10-04. Planning does not authorize scheduler preparation, submission,
execution, installation, or recovery of an existing live run. The operator will
announce renewed Cursor capacity before preparing and launching this feature run.
Its reviewer model and reasoning must be selected explicitly at submission.

## Non-Goals

- Detect quota renewal, change quota classification, or automatically retry all
  Cursor errors.
- Restore an agent's internal program counter or guarantee that its tools never
  repeat an external effect. The agent must inspect the existing partial work.
- Recover preflight, chat-creation, integrity, abort, or uncertain-process failures.
- Change models, review decisions, budgets, phase order, or accepted checkpoints.

## Scope

Extend the existing scheduler CLI with explicit forced recovery of a blocked
Cursor turn; authenticated recovery analysis; typed successor provenance and
protected prompt artifacts; standalone and sequence publication/reconciliation;
exact-chat dispatch; safe inspection output; historical compatibility; automated
tests; and current documentation and directly affected governance contracts.

## Out of Scope

Do not change `ai_dev_loop.yaml`, planning/reviewer skills, installed integrations,
provider binaries/configuration/authentication, systemd timers, lingering, remote
Git, or live runs and their artifacts. Do not edit `ai_dev_loop_hub` or its
Phase 8 execution worktree. Do not add a daemon, a new YAML option, a top-level
`recover`/`resume` command, generic rollback, or automatic forced recovery.
Ordinary timeout retries and usage-limit continuation retain their current
behavior. Existing review recovery must retain its evidence requirements.

## Required Context

Planning baseline: clean `main` at
`b516abdc7aa6582e17ad88a6c4ee94c3896499a9` (Phase 22 automatic Codex routing retries).
Recheck the checkout and the following prerequisites before dependent work;
archived plans alone do not establish an implemented capability.

Paths below are relative to `src/ai_dev_loop/` unless otherwise stated:

- `cli.py`: existing `scheduler cursor-retry`, without `--force`, delegates to
  `scheduler/application/cursor_timeout_retry.py` and only retries authenticated
  terminated timeouts in the supported waiting/ready states.
- `scheduler/application/cursor_workflow_service.py`, `cursor_evidence.py`,
  `attempt_service.py`, and `scheduler/cursor_attempt_runner.py`: authenticated
  turn outcomes, blocked failure ingestion, original/correction prompt binding,
  exact-chat resume, and successful staging/review dispatch.
- `scheduler/domain/state.py`, `events.py`, and `reducer.py`: a blocked snapshot
  does not contain the full Cursor/Codex/admission checkpoints. Recovery needs
  authenticated journal and attempt evidence, not invented `blocked.cursor` data.
- `scheduler/application/review_recovery.py`, `sequence_review_recovery.py`,
  `sequence_reconcile.py`, `sequence_lineage_ops.py`, `sequence_lineage_projection.py`,
  and `scheduler/infrastructure/sequence_run_lineage_store.py`: existing successor
  publication, sequence replacement, CAS, reservation and projection mechanics.
  Review recovery itself requires a completed staged implementation and bound B;
  these are not prerequisites of the failed initial Cursor turn.
- `scheduler/domain/sequence_execution_replacement.py`: the existing intent is
  review-specific and requires a staged patch and reviewer. Do not fill those
  fields with placeholders for Cursor recovery.
- `scheduler/domain/sequence_run_lineage.py` and corresponding v1 schemas: current
  attempt kinds are `planned_run` and `same_reviewer_retry`; recovery needs an
  honest Cursor-specific kind, including before B exists.
- `scheduler/infrastructure/sqlite_store.py`, protected artifact store, attempt
  services, tick fencing, abort and sequence checkpoint services: durable
  authority, migration, publication and exact process ownership.
- `integration_api/models.py`, `sequence_projection.py`, and
  `schemas/integration-api-sequence-shared-v1.json`: public `attemptKind` is an
  open nonempty string. Projecting a new kind does not require changing the
  Integration API major or introducing remote control endpoints.

Read current `docs/referencia/cli.md`, `integration-api.md`,
`trazabilidad-fases.md`, and `docs/operacion/estado-artefactos.md`,
`prepare-start-resume-abort.md`, `troubleshooting.md`, and
`seguridad-privacidad.md`. Read the existing timeout, correction, sequence review
recovery, lineage, and Phase 22 tests listed in Validation.

Motivation: a Phase 8 sequence stopped on a failed initial Cursor turn when the
operator reported depleted quota, leaving tracked and untracked partial work and
no reviewer. This report is not a machine quota classification or a test fixture.
Use synthetic fake-agent scenarios, not live source artifacts.

## Cursor Rules And Skills

Follow `AGENTS.md` and all relevant repository rules:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc` for privacy,
  public projections, and the prohibition on real installation side effects.

There is no `.cursor/skills/`. The plan was authored using
`.agents/skills/create-cursor-plan/SKILL.md`; implementation must not edit it.
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md` governs the separate
read-only reviewer. It does not authorize Cursor to review or accept its own work.

This approved feature extends the rules' timeout-only Cursor retry scope and
review-specific successor scope. Update only their affected statements and
`AGENTS.md` to describe the implemented exception. The original implementation
and reviewer-authored correction instructions remain exact; the fixed recovery
note is a separately authenticated operational envelope, not new findings.

## Architecture Guardrails

- The central ledger, typed journal, protected artifacts, and owned attempt
  evidence remain authoritative. No direct SQLite edits, legacy state mutation,
  guessed identity, `--last`, transcript search, or classification from prose.
- Keep the source blocked snapshot, journal, submitted inputs, and attempt
  artifacts unchanged. Record successor relation and recovery authorization
  separately; do not rewrite the source failure as a usage-limit event.
- `--force` supplies explicit operational authorization; it bypasses neither
  integrity, process ownership, abort, reservation, nor sequence CAS checks.
- Apply no new clean-worktree admission gate to expected partial implementation.
  Preserve staged, unstaged, untracked files and index contents during recovery
  authorization. No reset, clean, stash, rollback, or preliminary `git add`.
- A blocked Cursor failure currently releases its run reservation. Authenticate
  ownership and safely reacquire/transfer the reservation using the supported
  recovery boundary; do not assume the blocked source still holds it. Refuse a
  conflicting owner and retain ownership during uncertain publication.
- Freeze command, Cursor model/options, reviewer model/reasoning/sandbox, original
  inputs, logical turn/review iteration, completed-review count, and separately
  authorized effective review ceiling. Recovery itself consumes no review.
- Reuse B exactly if previously authenticated; if B genuinely does not yet exist,
  create it once at the normal first-review boundary. Missing evidence for a
  previously created reviewer is not permission to create another.
- Keep domain validation separate from CLI formatting, SQLite, artifact I/O and
  subprocess adapters. Launch through existing fenced effects and ticks only.
- Default output may expose run/sequence IDs, counts, safe reasons and artifact
  references, never raw prompt/output, patches, credentials or full agent IDs.

## Implementation Plan

Implement the following contracts in one run, in dependency order.

### 1. Authenticate the failed turn and define recovery authority

**C-01 — Eligibility and command routing.** Add `--force` to the existing CLI.
Without it, preserve the existing timeout service, result shape, identity and
prompt behavior. With it, support only a blocked `cursor_failure` whose decisive
failed attempt is an authenticated, terminal, nonzero `cursor.run_turn` belonging
to that source. The command performs no agent launch. Reject other blocked
reasons, waits, successes, unknown runs, create-chat failures, missing chat or
prompt, corrupt bindings, unresolved process/checkpoint holds, durable abort,
and conflicting reservation ownership. Do not guess the latest attempt from a
timestamp or accept an unrelated earlier failure. Use existing ownership and
completion checks to prove the failed unit terminated; an exit message alone
cannot override an unresolved hold.

**C-02 — Checkpoint continuity.** Implement a Cursor-specific recovery analyzer
using authenticated invocation/outcome evidence and the journal preceding the
failed turn. Recover the admitted context, exact chat, failed prompt binding,
logical iteration, reviewer checkpoint when present, counters, and effective
ceiling. Support both first implementation and correction, standalone and
sequence. Follow authenticated successor provenance when the source itself is a
recovery successor; do not assume all prior reviews occurred under its run ID.
Do not invoke admission again or fabricate a staged implementation/reviewer.

### 2. Create the exact operational prompt and typed provenance

**C-03 — Exact prompt plus one note.** The base is the exact prompt artifact
bound to the failed invocation, including any existing execution envelope.
For a correction this can be the correction envelope, not merely the raw
`cursor_fix_prompt`. Never use a mutable repository prompt or regenerate a fix.
Preserve the authenticated UTF-8 artifact bytes, append exactly two LF bytes
and this fixed note, with one final LF:

```text
Recovery note: A previous attempt of this turn was interrupted and may have left partial work in this repository. Inspect the existing changes and continue according to the original plan and instructions. Preserve the work already present; determine what is complete and what still needs implementation or verification. Do not reset or discard existing work merely to start over.
```

Write a new private envelope and hash; never modify the original artifact.
An existing Phase 23 envelope is recognized only by authenticated typed
provenance, not by searching for matching text. On recovery of another failed
recovery turn, reuse that provenance's original base plus exactly one note.
Preserve raw fix instructions byte-for-byte. Bind source failed attempt,
base/effective hashes, exact chat and logical iteration to dispatch evidence;
update validators and runner together, retaining the ordinary exact-argv path.

**C-04 — Versioned persistence and compatibility.** Add a dedicated v1 typed
Cursor recovery record with corresponding schema and private artifacts. It must
bind source/successor, failure attempt and authenticated evidence references,
base and effective prompt hashes, chat binding, turn kind/iteration and restored
checkpoint provenance. Carry reviewer checkpoints as explicitly tagged
`not_created` or `bound`; the bound form requires authenticated identity evidence.
Required fields cannot be absent/null; absence is supported only in retained
historical formats, never inferred as new recovery authorization. Use canonical
JSON integers, reject booleans/string coercions for new counters/versions, and
retain the historical lineage number behavior in its existing readers.

Add `cursor_retry` as a generation >= 2 attempt kind with explicit source run.
Keep generation 1 `planned_run`, contiguous generations, immediate predecessor,
unique runs and current-leaf acceptance invariants. Add v2 lineage/phase/attempt
schemas and compatible readers; preserve genuine v1 planned/review-recovery
documents under their original schemas. New Cursor attempts must not validate as
v1 review retries. Add a separate typed v1 Cursor sequence replacement intent,
without mandatory reviewer/staged-patch fields, and route its publication through
the supported recovery reconciliation boundary. Keep existing review intent v1/v2
semantics unchanged. Update required migration, stores, dispatch readers and
projections together, using a unique source/failure recovery key. Public
`attemptKind` reports `cursor_retry` through its existing open-string contract;
do not infer the kind from generation or invent a reviewer.

### 3. Publish one successor and converge through restart/abort

**C-05 — Idempotent standalone publication.** After eligibility verification,
durably claim one recovery intent and publish exactly one tick-eligible successor
with copied/authenticated frozen inputs, restored checkpoints, same worktree/chat
and new private effective prompt. Authorize only the retry of the failed turn.
Make repeated/concurrent commands return the same successor, including commands
addressing the original source after that successor has advanced. No repeated
authorization may recreate an effect, relaunch a completed turn or require that
the original source is still the current leaf. Authenticate an existing relation
before returning it. A newly failed successor can be explicitly recovered by its
own ID and failure key; this must not reset budgets or accumulate notes.

Precondition: eligible evidence, resolved process ownership, no cancellation,
available reservation. Durable authority: recovery intent, successor link and
fenced effect. Permitted effect: protected artifact publication, reservation
acquisition/transfer and one retry dispatch. Postcondition: exactly one runnable
successor or a durable unresolved publication owned by reconciliation. Reconcile
interruption before/after artifact writes, ledger publication and reservation
transfer. Until uncertainty resolves retain the reservation/hold; then converge
to an authenticated runnable successor or completed cancellation, without leaks
or duplicate launches.

**C-06 — Original sequence continuation.** For a new sequence recovery,
authenticate frozen sequence entry, ordinal, hash, blocked current leaf and
generation. Publish the successor and update current-run lineage, reservation,
sequence state and retry effect through the existing transactional CAS and
publication protocol. Preserve sequence ID, phase ID/order and frozen remaining
entries. Recovery does not accept the phase or create its checkpoint commit.
After the retried turn succeeds, ordinary staging/review acceptance authorizes
the ordinary non-final checkpoint and next phase; final phases reach ordinary
finalization with changes staged. Replace review-only kind assumptions in actual
checkpoint/store/reconcile callers, not just in status rendering.

Abort wins before dispatch and during replacement publication. Do not revive a
stale leaf or adopt a successor after cancellation. With uncertain publication,
retain ownership; after restart reconcile publication and abort to a definitive
state and release only when existing cleanup conditions permit. Reuse generic
publication mechanics without weakening review recovery's eligibility.

### 4. Dispatch, inspect and document the supported flow

**C-07 — Observable end-to-end flow.** Extend attempt binding/validation to resume
the original Cursor chat with the effective prompt, never `create-chat`.
On successful completion use normal `stage_mode: all` normalization, reviewed
snapshot and Codex workflow. Do not expose partial work to review before Cursor
succeeds. Initial recovery bootstraps B normally; correction recovery resumes B
and preserves completed-review accounting and exhaustion/extension behavior.

Forced text/JSON results must identify source, successor, whether authorization
changed, optional sequence/ordinal, and the supported safe next action. Use
existing receipt/next-action conventions and preserve legacy no-force output.
Status/history/timeline and sequence reports show factual provenance. CLI help
and current Spanish docs explain eligibility, same-chat initial/correction
recovery, immutable blocked source, appended note, dirty-work preservation,
duplicate behavior, sequence continuity, failure diagnostics, and recovery's
limits. Use placeholder run IDs in examples. Update only directly affected
rules/AGENTS statements; do not change control-plane skills or YAML.

## Testing Criteria

Automated unit, schema, integration and regression tests are mandatory. Use fake
`agent`/`codex`, isolated native-WSL temp HOME/XDG and repositories, injected time,
and existing process backends. Never use real provider calls, live scheduler
artifacts, timers or the operator's actual execution worktree.

| Contract | Required observable acceptance and production path |
| --- | --- |
| C-01 | Real CLI rejects forced recovery of every unsupported category above without successor, Git edits or launch. Existing no-force timeout success, exhaustion, prompt identity and duplicate tests still pass. A valid fake nonzero initial turn is blocked, rejected without force, and recoverable with force. |
| C-02 | Unit evidence tests plus actual ingestion/recovery verify initial/correction checkpoints, frozen models, exact chat/B, completed-review counts and an extended ceiling. Reject corrupted prompt/outcome/chat, mismatched attempt/nonce/unit and missing previously bound B. Recover a failed successor with authenticated cross-run provenance. |
| C-03 | Fake agent records actual argv prompt bytes and chat on retry. Independently assert original base + specified suffix, immutable source hashes, exact raw correction and no duplicate note on a second recovery. Include existing continuation/correction envelopes and multiline instructions. |
| C-04 | Models and JSON schemas agree on new supported forms and rejection of absent/null required fields, malformed hashes, unsupported versions and numeric coercions. Retain v1 planned/review lineage and v1/v2 review-intent fixtures captured from baseline writers, with provenance; new kind uses v2. Verify migration/reopen and open-string Integration API projection. |
| C-05 | Real service/CLI concurrency with explicit barriers produces one linked successor and one pending effect. Restart around artifact, ledger and reservation boundaries converges without duplicate turn or lost reservation. Duplicate source command after successor advancement returns existing relation. Check index tree, staged/unstaged diff and untracked sentinel bytes unchanged immediately after authorization. Conflicting owner remains untouched. |
| C-06 | Fake three-phase sequence fails initial Cursor in middle phase, recovers through the real CLI and ticks, accepts that ordinal, makes only its authorized checkpoint, starts the next phase and reaches finalization. Same sequence/ordinal, two attempt generations, source still blocked, accepted leaf is successor. Repeat with a failed correction and exact B continuity. Deterministic forced-retry/tick/abort races, stale-leaf rejection, uncertain publication ownership and eventual abort/restart convergence. |
| C-07 | Standalone and sequence retries succeed through actual runner, staging and review callers, preserving partial files. No new chat; no review before Cursor success. Counter unchanged at retry, increases only on authenticated completed review. Final/standalone changes staged; no unauthorized commit. Status/result/report/API summaries contain correct successor and kind without raw prompt, output or agent IDs. |

Suggested new tests: `tests/unit/scheduler/test_phase23_cursor_recovery.py`,
`test_phase23_cursor_recovery_concurrency.py`,
`tests/integration/test_phase23_cursor_recovery.py`,
`test_phase23_sequence_cursor_recovery.py`, and
`tests/fixtures/phase23_historical/`. Reuse actual historical writers/fixtures
from the planning baseline, not JSON newly generated by the changed models and
called historical. Test seams may inject crashes or process responses, never
perform missing production locking, publication, adoption or state changes.

The decisive regression must fail on the baseline: `--force` is unsupported and
the sequence cannot natively recover this blocked initial Cursor turn. Assertions
must prove eventual success, original sequence continuity and exact retry input;
merely observing a blocked outcome or helper invocation does not prove recovery.

## Validation

Use the existing isolated `.venv` (Python 3.11+) and development dependencies;
do not replace system Python. Run after implementation:

```bash
.venv/bin/python -m pytest tests/unit/scheduler/test_phase23*.py tests/integration/test_phase23*.py
.venv/bin/python -m pytest tests/integration/test_cursor_timeout_retry.py tests/integration/test_phase17_4_cursor_workflow.py tests/unit/scheduler/test_cursor_workflow_corrections.py tests/unit/scheduler/test_cursor_evidence_corrections.py tests/integration/test_phase20_8_sequence_review_retry.py tests/unit/scheduler/test_phase20_8_sequence_review_retry*.py tests/unit/scheduler/test_phase20_9_review_retry_barrier.py tests/unit/scheduler/test_phase20_7_sequence_run_lineage.py tests/integration/test_phase22_codex_routing_auto_retry.py tests/integration/test_phase22_sequence_routing.py tests/unit/integration_api/test_contract.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Check real CLI help with the local checkout, without authorizing a live run.
If test files are reorganized, report equivalent exact paths/commands. Summarize
C-01–C-07 implementation, production callers and test evidence, plus exact
executed/passed/failed/unexecuted commands. Mandatory missing behavior or coverage
is incomplete delivery, not residual risk. Do not install this implementation or
retry the live hub sequence as validation. Operator acceptance can later inspect
the fake recovery evidence and separately authorize installation/live recovery.

## Risks Or Recovery Notes

- The largest risks are checkpoint reconstruction, prompt provenance, reservation
  reacquisition and sequence publication across interruption. Keep this one run
  focused on the manual failed-turn flow; do not build a general recovery engine.
- Historical blocked sources are eligible only if retained authenticated evidence
  establishes the complete checkpoint. Missing evidence yields a safe diagnostic;
  do not backfill invented proof or automatically resubmit fresh work.
- The fixed note does not establish renewed capacity. If the provider still
  fails, retain the new attempt and partial work, block the successor normally,
  and require another explicit recovery of that successor.
- No command guarantees exact tool-action continuation inside the chat. Existing
  agent instructions and the note require inspecting already performed work.
- Planning artifacts are source inputs, not a submitted run. The prompt filename
  follows the repository's intentionally ignored `prompt_*.txt` convention and
  must remain available locally for the later approved handoff.

## OpenQuestions

None. Reviewer model/reasoning selection and renewed provider capacity are launch
inputs to be supplied later; they do not block authoring or define new recovery
policy. If implementation discovers conflicting prerequisites or an unresolved
safety decision, stop dependent work and report the evidence and needed decision.

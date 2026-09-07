# Phase 17.5 — Codex review and bounded scheduler loop

## Goal

Complete the scheduler's review loop: after a staged run reaches
`awaiting_codex_review`, its first review attempt creates one fresh, read-only
Codex CLI reviewer B with the model and reasoning effort frozen at submission.
The scheduler validates and persists B's exact identity, ingests only the
schema-validated review decision, then either reaches a terminal outcome or
schedules the exact Cursor correction turn. Later reviews in the same durable
run resume that one B identity.

Every agent remains externally owned by systemd; every scheduler invocation is
a short tick. B reviews only and never implements, dispatches Cursor, or mutates
Git state.

## Non-Goals

- Do not add GitHub/PR-review behavior, commits, pushes, or external writes.
- Do not implement cutover deletion, systemd enablement, or public abort beyond
  the already-existing low-level attempt cancellation capability.
- Do not use Markdown or an unvalidated stream event to decide workflow
  outcomes.
- Do not create a B at submit/start, fork a B, use `--last`, or create another
  B after a bootstrap/recovery ambiguity.

## Scope

- Add durable fresh-Codex-bootstrap, exact-resume, review-observe, and
  review-result-ingestion effects to the scheduler.
- Consume the immutable reviewer model/reasoning binding supplied by controller
  A at `scheduler submit`; persist no B identity until first review bootstrap.
- Capture one validated fresh B identity and reuse it for all later review
  iterations, with schema-valid review decisions and bounded Cursor correction
  loops.
- Add no-findings, residual-risk, max-iterations, and safe blocked/uncertain
  states to the scheduler reducer and redacted controller/status projections.

## Out of Scope

- Legacy command deletion, XDG cleanup, human/manual service activation,
  automatic recovery of an unknown process result, and PR-review v2 migration.
- Changing the direct non-controller legacy interface before Phase 17.7.
- Model/reasoning fallback, YAML/default inheritance, or a new reviewer per
  correction pass.

## Required Context

Read the master and Phases 17.1, 17.1.5, 17.1.75, and 17.2–17.4, all Cursor
rules, `AGENTS.md`, `runners/codex.py`, `review_result.py`,
`response_schema.py`, `workflow_engine.py`, `iterations.py`,
`local_review_loop.py`, `tests/integration/test_start_codex_review.py`, and
bounded-loop/resume tests. Read the fake Codex creation-event characterization
and migration decision made in Phase 17.1.5 before altering any scheduler state.

## Cursor Rules And Skills

All `.cursor/rules/*.mdc` and `AGENTS.md` apply. In particular, the fresh B
identity contract, frozen command-selected model/reasoning, read-only sandbox,
argv ordering, structured result schema, review privacy, immutable
Codex-authored fix prompt, and no-continuous-worktree-policing constraints are
mandatory. Do not invoke the staged-review skill during implementation.

## Architecture Guardrails

- **One B per run:** the first review runs new `codex exec`; it must emit one
  strict structured creation identity before result ingestion. Persist it
  atomically with protected evidence. Every later review uses `codex exec resume
  <stored-id>` only. Never use `--last`, a fork, a Desktop session, or a second
  new reviewer.
- **Frozen command configuration:** each fresh and resumed command receives the
  exact model and reasoning effort frozen by `scheduler submit`; neither can be
  reread or defaulted from YAML or a runtime session. Reviewer identity and raw
  command data remain redacted outside protected artifacts.
- **Read-only review boundary:** fresh and resume invocations use argv arrays,
  explicit CWD, `--sandbox read-only`, bounded stdin/stdout/stderr, JSON, and
  the approved output schema. A review prompt is not a substitute for sandbox
  enforcement. B must not stage, dispatch, commit, or modify the worktree.
- **Structured authority:** only a valid `codex-review-result-v1` result drives
  findings, test status, residual risk, fix prompt, and terminalization.
  Markdown and creation events are audit evidence only after identity validation.
- **Recovery:** a stored B identity is resumed exactly. Missing, malformed,
  conflicting, or uncertain fresh-creation identity blocks the run and preserves
  artifacts; it is never a reason to create another B. Late results remain
  fenced stale evidence.
- **Agent-led normal flow:** after preflight admission, preserve the reviewed
  Cursor/Codex exchange and explicit review decisions. Do not add broad Git
  drift gates beyond the approved reservation, staged-artifact, and correction
  preconditions.

## Implementation Plan

1. Reuse the Phase 17.1.5 fresh-Codex adapter contract rather than duplicating
   parsing, redaction, or command construction. Adapt it to Phase 17.3 durable
   attempts: explicit `bootstrap_codex_review` and `resume_codex_review` effects
   have distinct immutable invocation hashes and the same review-result parser.
2. Add scheduler states/events for unbound reviewer, bootstrap launch/observe,
   validated identity binding, resume launch/observe, result ingestion,
   `waiting_for_cursor_fix`, no-findings completion, residual-risk completion,
   max-iterations terminalization, and typed bootstrap uncertainty.
3. On the first `awaiting_codex_review`, construct fresh `codex exec` argv from
   the immutable binding, launch through the Phase 17.3 backend, validate its
   machine-readable new-session identity, and persist it before scheduling any
   later review. On subsequent review boundaries, construct only exact resume
   argv from the stored identity. Do not allow an adapter fallback between modes.
4. Ingest only a schema- and cross-field-validated review result. Preserve its
   protected raw artifacts and, when findings exist, forward the exact stored
   Codex-authored fix prompt to the existing Phase 17.4 Cursor/staging effects.
   The scheduler may envelope it but may not rewrite it.
5. Preserve review budgets: review 01 follows initial Cursor; review N findings
   schedule correction N+1 only below max; at limit terminalize without another
   Cursor or Codex attempt. Status/controller views report review-active,
   reviewer-bound (without ID), blocked bootstrap, waiting fix, and terminal
   outcomes without sensitive content.
6. Update active docs only for fake-backed behavior now proved. State that B is
   created once at first review with submit-frozen parameters and resumed after;
   retain cutover and service-install claims for later phases.

## Testing Criteria

- **Fake-Codex command contract:** initial review verifies new `codex exec` argv
  with `--sandbox read-only`, JSON/output schema, exact frozen model/reasoning,
  and no resume/fork/`--last`/ephemeral option. Its fake structured event yields
  one captured B ID. The next review verifies `exec resume <that-id>` and the
  same configuration; it must not receive a new B ID.
- **Persistence/recovery:** test crash before launch, after launch intent, after
  fresh process completion but before identity binding, after binding before
  review ingestion, and after review ingestion. Ambiguous/missing/conflicting
  identity always blocks with evidence; a persisted ID always resumes exactly;
  late/duplicate results cannot bind or replace B.
- **End-to-end fake scheduler:** no findings; findings then one correction;
  repeated findings to max; residual tests status; missing/empty fix prompt;
  staged drift before correction; usage limit during correction; stale
  Cursor/Codex completion fences; and tick exits while a fake agent is active.
- **Safety/privacy:** assert no subprocess at submission, no YAML/runtime
  fallback, no reviewer Git/dispatch route, redacted status/history, bounded
  protected artifacts, and no real Codex, Cursor, systemd, model, or user-global
  service use.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/test_codex_runner.py \
  tests/unit/test_response_schema.py \
  tests/unit/test_iterations_and_resume.py \
  tests/integration/test_start_codex_review.py \
  tests/integration/test_phase5_loop_resume.py \
  tests/integration/test_phase17_5_*.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
git diff --check
```

## Risks Or Recovery Notes

The serious failure is splitting B's review continuity: a retry that makes a
second new B loses the reviewer context and can turn unrelated review history
into a valid decision. The first creation identity is therefore a durable fence,
not a convenience. A partial process result or Markdown report cannot bypass it.

## OpenQuestions

None. Phase 17.1.5 fixes the compatibility policy: direct legacy remains
unchanged until cutover, and prior session-bound contexts are read-only and
execution-blocked rather than converted.

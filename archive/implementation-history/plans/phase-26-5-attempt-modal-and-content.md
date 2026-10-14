# Phase 26.5: Attempt modal and explicit content inspection

Status: operator-requested implementation scope; implementation pending.
Sequence position: 5/7. Cursor: grok-4.7-high.
Reviewer: gpt-6.1-sol / high, selected by the operator on 2026-10-14.
One ordinary automatic run, with accepted predecessor dependency and one final
operator walkthrough in 26.7. Planning does not prepare/submit/start the sequence.

## Goals

Open metadata for the exact clicked attempt and deliberately load its prompt context, stdout or stderr through existing authenticated artifact readers.

## Non-Goals

Automatic content refresh, downloads, raw artifact filesystem browsing or reconstruction of missing per-attempt prompts.

## Scope

Accessible modal, exact resource targeting, prompt ownership labels, bounded byte-window readers, active/truncated/missing/integrity states and buffer retirement.

## Out of Scope

Scheduler artifact writers, new Cursor prompt capture, review identity/result schema changes, provider transcripts, browser/storage/content caching and Hub adapters.

## Required Context

Planning baseline is `main` at `90262eed1af7c76c46139855e249de4ea8ec0d5e`, observed 2026-10-14:
Phase 23 production plus planning-only Phases 24/25 and the non-interactive prompt
fix. Neither 24 nor 25 is a dependency. At this planning baseline, SQLite schema
is 13 and local API is 1.5, before 26.1 adds dashboard capability/additive minor.
Python >=3.11 and `uv` are the current runtime/tooling.
Recheck relevant accepted code once at admission. A future predecessor plan or
staged patch is not evidence of accepted runtime. If a producer changed, stop
only dependent work and report its exact incompatible/missing contract.

Read [Phase 26 overview](phase-26-local-dashboard.md),
[shared dashboard contract](phase-26-dashboard-contract.md),
[source inventory](phase-26-source-observations.json),
[visual reference](phase-26-reference/README.md), `AGENTS.md`,
`docs/referencia/integration-api.md`, `docs/referencia/cli.md`,
`docs/operacion/estado-artefactos.md` and `docs/operacion/seguridad-privacidad.md`.
The referenced new paths below are implementation outputs, not current prerequisites.

Actual accepted 26.4 exact run/attempt selection and 26.2 typed content routes.
Existing `integration_api/artifact_reader.py`, `review_reader.py`, `review_service.py`,
`process_output_service.py`, `process_output_reader.py` and ownership/evidence
resolvers are the production content source. Read genuine pre-21.4 historical
prompt fixtures and current active/complete output tests. Exact Cursor correction
attempt envelope is not a new available public contract; preserve this limit.

## Cursor Rules And Skills

Follow `AGENTS.md` and these repo-local rules:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-dashboard-contracts.mdc`

Read `.agents/skills/create-cursor-plan/SKILL.md`; for documentation/evidence,
read `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`, applying its
current source/privacy requirements rather than historical Phase 8 restrictions.
The separate final reviewer follows
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`; implementation does
not invoke or modify it. There is no `.cursor/skills/` directory. Codex authored
the new dashboard rule/shared contract; Cursor does not change those policies,
planning/review skills or reviewer/correction/result schemas to pass acceptance.

## Architecture Guardrails

One local installation owns the data. SQLite reads use the read-only store and
validated snapshots/lineage; the new SQL read adapter belongs inside ai_dev_loop.
No scheduler transition, migration, reservation, repair, Git, model turn, timer
or real workstation installation is a dashboard side effect. Initial execution
means the first owned execution launch request, excluding chat creation/probes;
project and lineage come from authoritative fields, not names. Keep public DTOs
separate from internal models and ECharts options. Enforce the shared contract's
query/page/envelope/time bounds; missing evidence and partial pages are explicit.

The local server has no login and binds only 127.0.0.1 with typed same-origin read
routes. Sensitive content uses existing confined, verified, paginated readers,
loads only through deliberate UI actions, remains memory-only and is not logged.
UI components use DashboardDataSource; transports/SQLite never own chart logic.
Requests and content retire on scope/selection changes; old responses cannot
restore data. Preserve existing major compatibility and unsupported historical
semantics. Capacity is current, independently dated, and never time-filtered.

Keep root ai_dev_loop.yaml, Phase 24/25 plans, scheduler execution/recovery,
installed providers/drivers, user integrations and live source ledger/artifacts
unchanged. A stable installed scheduler driver must run this self-hosted sequence;
using an editable changing checkout as the live implementation driver is outside
scope. Explicit sequence start authorizes only existing non-final checkpoint
commits; agents do not create their own commits, push or final acceptance.

The shared contract fixes transport parameters/DTOs, metric ownership, lifecycle
and explicit safety bounds; complete this plan's owned slice without broadening
adjacent work. Read the exact public/shared types rather than reproducing rules
from display strings. Any incompatible prerequisite is a dependent-work blocker.

## Implementation Plan

1. Add modal from selected attempt metadata with runId, attemptId, component,
   effect, iteration/phaseAttempt, status and observed times. Opening shows facts
   already obtained by summary/timeline and makes zero prompt/review/output read.
   Support focus trap, Escape, close button and returning focus to the selected bar
   or companion row. Changing run/filter closes and retires it.
2. Add deliberate prompt/stdout/stderr controls. Codex review prompt requests its
   exact runId/attemptId through existing review-content; optional availability
   detail is loaded only with that deliberate inspection. Cursor initial prompt
   is labelled Initial run prompt (context); do not claim the exact turn envelope.
   Show exact correction prompt as unavailable without a public binding. An
   explicit review-owned cursor-fix-prompt keeps that review ownership visible.
3. Implement shared UTF-8 incremental byte-window adapter for 65,536-byte chunks,
   <=1 MiB raw and <=2 MiB decoded-text window. Load-more/next-window and literal
   search stay bounded. Text is escaped; no HTML/Markdown execution. Keep pending
   multibyte decoder state across chunks, reset when ownership/window changes.
4. Preserve available/not_recorded/absent, complete vs hasMore, active EOF,
   nextOffset, source truncation and integrity/replacement failure independently.
   Checking new output bytes is explicit; no polling/provider call. Errors expose
   safe codes; failed integrity retires old content rather than merging new bytes.
5. Use generation + AbortController retirement for close/reselect/filter/route;
   release raw/decoded/search buffers, do not persist/log them. A late success
   after close cannot resurrect the reader. At most one content target/stream is
   active in this modal and it participates in bounded data-source admission.
6. Add fake-backed real reader/server/browser tests and record
   `archive/implementation-history/findings/phase-26-5-attempt-modal-and-content.md`.

## Testing Criteria

Mandatory contracts are the acceptance boundary. Add/update automated unit,
contract/integration and browser tests as listed; test the real producer/consumer
with independent expected outputs, not a duplicate implementation or a test-only
safety guard. IDs belong to this run and must appear in its completion report.

| ID | Real producer -> consumer | Input/precondition | Independent expected result / defect caught |
| --- | --- | --- | --- |
| C265-1 | Timeline click -> metadata modal -> deliberate content request | Known exact attempt plus prompt/output sentinels; open then press one content control; click a different attempt. | No content call on open; chosen action targets the exact attempt/stream and initial Cursor prompt has contextual ownership label. Catches accidental prefetch and guessed correction binding. |
| C265-2 | Existing verified artifact service -> HTTP -> window decoder | Historical prompt not_recorded, wrong ownership, UTF-8 boundary, >1 MiB output, active EOF, complete with hasMore and truncated/replaced stream. | Expected bytes/metadata/error behavior and bounded windows; no fake exact prompt, completion conflation or replacement merge. Catches decoding each chunk independently and serving runner stdout as process output. |
| C265-3 | In-flight reader -> modal retirement -> late reply | Hold content response, then Escape/reselect/filter/route leave, with synchronized release. | Request retires, buffers/search cleared, modal/focus correct and late reply never reopens; no bodies in storage/logs/traces. Catches stale sensitive content leaking to a new attempt. |

## Validation

Create `dashboard-web/src/dashboard/AttemptModal.test.tsx`,
`ContentReader.test.tsx`, `dashboard-web/e2e/attempt-content.spec.ts` and a focused
HTTP content regression selector in tests/integration. Frontend cwd:

```bash
npm run typecheck
npm test -- --run src/dashboard/AttemptModal.test.tsx src/dashboard/ContentReader.test.tsx
npm run build
npx playwright test e2e/attempt-content.spec.ts
```

At root run affected HTTP/content tests and existing
`tests/unit/integration_api/test_process_output_auth.py`,
`test_process_output_resolution.py`, `test_review_evidence_auth.py`,
`test_artifact_reader.py` with pytest, plus `uv run mkdocs build --strict` and
`git diff --check`. Use synthetic owned captures and historical fixtures; never
record real operator prompts/output in browser traces/screenshots/videos/evidence.

These are planned implementation checks, not results of plan authoring. Use
isolated temporary XDG/homes/repos and fake providers; tests must not launch real
Cursor/Codex model turns, tick/control the live scheduler or change installed
services. Report exact commands, exit codes, elapsed time, executed/failed/unrun
checks and every contract ID -> production caller -> independent assertion ->
result in the owning findings file. Mandatory missing behavior/tests remain
incomplete scope. After correction rerun failed/affected checks; expand only for
a concrete remaining risk. Full repository pytest belongs once to 26.7's
integration gate, not to every run/review. Target <=40 minutes focused validation
per subphase, keeping setup/cleanup owned and bounded. A failed required check
blocks dependent work; informational READY_FOR_REVIEW/BLOCKED_CONTRACT labels
are not new scheduler states.

## Risks Or Recovery Notes

Read-only permits explicit display of sensitive captured content locally;
no claim is made that arbitrary captured text is secret-free. Preserve confinement,
ownership and on-demand policy. No new per-attempt Cursor writer/schema is
included; missing public evidence stays unavailable. Reverting the UI does not
alter historical artifacts.

## OpenQuestions

None.

Product scope and reviewer choices are defined at authoring. Missing actual accepted predecessor,
incompatible contract or unavailable mandatory validation environment found at
admission is a new concrete execution blocker: report the evidence and required
decision; stop dependent work without inventing a private fallback.

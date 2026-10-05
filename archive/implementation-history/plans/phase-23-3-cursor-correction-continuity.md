# Phase 23.3 — Correction recovery with reviewer, budget and evidence continuity

## Goals

Extend accepted standalone forced recovery to failed Cursor correction turns.
Preserve exact reviewer B, the completed-review count, separately authorized
ceiling, raw fix and failed invocation envelope through authenticated ancestry.
Keep the Phase 23.2 publication/abort mechanism and initial recovery working.

## Non-Goals

- Adopt successors into a sequence, switch B/models or increase a review budget.
- Reconstruct missing proof by guessing identities, counters or fix instructions.
- Make forced retry automatic or recover active/aborted/integrity failures.

## Scope

Correction eligibility and cross-run ancestry; v2 recovery record/schema;
authenticated budget carry; correction prompt/attempt validation; compatibility
with accepted v1 initial recovery and existing review recovery; real standalone
recovery tests and current operational documentation.

## Out of Scope

No sequence replacement or lineage schema changes, remote APIs/control, timer or
provider configuration, live run recovery, hub work, YAML or planning/review skill
edits. Do not extend budgets by changing frozen submitted context.

## Required Context

Read [the overview](phase-23-replanned-overview.md),
[23.1](phase-23-1-cursor-recovery-evidence.md),
[23.2](phase-23-2-initial-standalone-cursor-recovery.md), `AGENTS.md` and current
CLI/state/recovery/privacy docs. Verify both predecessors are accepted in the
checkout: the analyzer, exact-note v1 record, idempotent standalone publication,
ready retry effect, protected input copying and actual restart/abort paths.
Their existence cannot be inferred from the combined aborted patch.

Baseline paths under `src/ai_dev_loop/`: scheduler `application/review_budget.py`
folds authenticated ordered extension events separately from frozen config;
`codex_workflow_service.py`, `codex_evidence.py` and `codex_argv.py` authenticate B,
review decisions and exact resume; `attempt_service.py` binds correction envelope,
raw fix and staged-patch evidence. `cursor_workflow_service.py`/`cursor_evidence.py`
validate turn outcomes, and `review_recovery.py` resolves existing review-recovery
ancestry. `domain/state.py` contains cursor/codex checkpoints; blocked snapshots
do not retain them. Recheck actual production types/callers plus the accepted
23.2 record/publication implementation. Read correction evidence/workflow,
review-budget extension and existing review-recovery tests.

## Cursor Rules And Skills

Follow `AGENTS.md` and these files under `.cursor/rules/`:
`ai-dev-loop-governance.mdc`, `ai-dev-loop-orchestrator-contracts.mdc`,
`ai-dev-loop-state-and-schema-contracts.mdc`,
`ai-dev-loop-loop-and-resume-contracts.mdc`,
`ai-dev-loop-codex-review-contracts.mdc`, `ai-dev-loop-abort-contracts.mdc`,
`ai-dev-loop-docs-acceptance-contracts.mdc`, and
`ai-dev-loop-global-integrations-contracts.mdc`.
There is no `.cursor/skills/`. Keep
`.agents/skills/create-cursor-plan/SKILL.md` and the separate read-only reviewer
skill `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md` unchanged.
Update only directly affected rules/AGENTS statements with the accepted slice.

## Architecture Guardrails

- Reuse accepted 23.2 publication and abort authority; add no second scheduler
  or generic recovery state machine.
- Bind every cross-run evidence reference to its actual owner run, artifact hash
  and authenticated relation. Never rewrite historical invocation run/unit IDs
  to make old proof appear to belong to a successor.
- Review results and authenticated checkpoints determine completed counters;
  findings prose and the number of attempted processes do not.
- Keep original submitted limits unchanged. Carry effective ceilings through
  explicit authenticated recovery provenance, then apply successor-local
  extension events normally. The operator's `scheduler extend` remains the only
  authority to raise an effective ceiling.
- A correction requires a proved bound B. Missing/uncertain B is not equivalent
  to `not_created`, and `--last`/fresh bootstrap cannot repair that gap.
- Sequence sources still reject mutation clearly; ordinary sequence and reviewer
  retry behavior retain their existing contracts.

## Implementation Plan

**C-01 — Authenticated causal checkpoint and ancestry.** Extend the accepted
analyzer to standalone corrections and supported Cursor/review-recovery ancestor
relations. Resolve the decisive failed turn from ordered authenticated causal
evidence as in 23.1; collect the exact preceding admitted/cursor/codex checkpoints,
raw fix, correction envelope, previous staged/review evidence, authenticated B
bootstrap/binding, completed-review count and effective ceiling. An ancestor
chain is explicit, acyclic and validated at each edge. Do not assume chat/B were
created or all reviews/extensions recorded in the current source run. Handle
history pagination without silent truncation. Reject missing/conflicting evidence,
cycles, unrelated owners and earlier failures that did not cause this block.

**C-02 — Versioned record and review-budget carry.** Introduce complete v2
Cursor recovery record/schema with an explicit initial/correction discriminator,
required checkpoint/evidence bindings and reviewer form `not_created` or `bound`.
Bound form requires authenticated session evidence, not nullable dummy fields.
Initial v1 records remain valid only under their original supported form; capture
fixtures from accepted 23.2 writers with commit provenance. New writers emit v2;
readers retain v1 without manufacturing B or budget history. Preserve strict
required/null/hash/numeric rules and the actual record-byte digest binding.

Persist a typed, authenticated budget-carry authorization on successor publication
that references the parent authority and original extension evidence, records the
inherited effective ceiling and completed count, and leaves submitted workflow
configuration untouched. Update the actual `review_budget`/dispatch readers so
the inherited effective ceiling is their starting authority before successor-local
extensions. Validate monotonic original extension history, carried ceiling/count,
and inherited/local boundaries; do not relabel original extension events as new
operator decisions under a different run ID or count them twice. Preserve new
extensions and exhaustion behavior after recovery.

**C-03 — Exact correction execution and ordinary review.** Reuse the failed
invocation's full envelope as base, not a regenerated wrapper or raw fix alone.
Follow accepted 23.2's exact note/composition and authenticated deduplication;
when recovering a failed manual successor, retain its canonical original base
and one note. Store raw B-authored fix bytes unchanged. Update invocation binding,
pre-execution/evidence validators, current-run input copies and outcome ingestion
together so the actual runner accepts the envelope with explicit source
provenance. Restore the same logical iteration and exact B; on Cursor success,
normal staging and exact-session Codex resume perform the next review. No completed
review is consumed by authorization or a failed turn. Reject corrupt replay records
before returning a supposedly reusable successor.

**C-04 — Complete lifecycle and neighboring recovery compatibility.** Use 23.2's
unique source/failure relation and non-dispatchable publication guard for initial
and correction forms. Publication ready requires checkpoint, budget carry and
prompt proof as well as the runnable retry effect. Repeated calls after successor
advancement return the authenticated existing relation. Another failed successor
can recover using its own causal failure without resetting budget or growing
notes. Actual tick/restart/abort paths must retain holds during uncertainty and
converge to one ready dispatch or cancellation. Keep existing same-reviewer retry
and budget extension working when they follow a successful Cursor-recovery
successor; generalize ancestry helpers only where these real callers require it.
Update `--check`, help and current Spanish CLI, state, recovery, troubleshooting
and privacy docs for both standalone turn types and explicit sequence exclusion.

## Testing Criteria

Mandatory unit/schema and real CLI integration tests, suggested
`tests/unit/scheduler/test_phase23_3_correction_recovery.py`,
`test_phase23_3_budget_ancestry.py`,
`tests/integration/test_phase23_3_correction_recovery.py`, and genuine accepted
v1 fixtures under `tests/fixtures/phase23_3_historical/`. Use fake CLIs/backends,
injected clocks, isolated native-WSL HOME/XDG/repos and deterministic race barriers.

| Contract | Independent observable acceptance |
| --- | --- |
| C-01 | Actual initial implementation -> fake B findings -> failed correction -> real force -> resumed exact B after Cursor success. Evidence tests with prior recovery ancestors, same/reversed timestamps, paginated history and unrelated attempts select the causal failure or reject. Missing prior B never bootstraps a replacement. |
| C-02 | Real extension case: submitted ceiling 7, authorized ceiling 9, eight completed reviews and a failed correction preceding review 9. Force preserves 7 in submitted config, 9 effective, eight completed; next authenticated review counts once. Extend/exhaustion remains correct after another successor. Tamper parent/extension/carry evidence and reject. Original v1 initial fixtures still load; v2 correction requires bound evidence and forbids incomplete/null/coerced fields. |
| C-03 | Fake runner captures independently expected exact failed envelope + suffix and unchanged raw fix; same chat/B/models and iteration. Original source and artifacts unchanged. Missing/corrupt fix, staged evidence, record digest or wrong artifact owner rejects dispatch/replay. Second manual failure retains one note and correct origin. |
| C-04 | Cross-run correction chain and an existing review-recovery ancestor complete through production services. Cursor-recovery success followed by eligible review retry/blocked-review recovery and budget extension remains correct. Crash, concurrent retry, abort and restart preserve budget/B, ownership and one dispatch. Existing initial-only slice and no-flag timeout checks pass. |

Do not have test helpers copy missing production artifacts, set reviewer IDs,
repair counters or inject the publication guard themselves. Demonstrate actual
authenticated progression and finished acceptance, not merely a blocked outcome.

## Validation

```bash
.venv/bin/python -m pytest tests/unit/scheduler/test_phase23_3*.py tests/integration/test_phase23_3*.py
.venv/bin/python -m pytest tests/unit/scheduler/test_phase23_1*.py tests/unit/scheduler/test_phase23_2*.py tests/integration/test_phase23_1*.py tests/integration/test_phase23_2*.py tests/unit/scheduler/test_cursor_workflow_corrections.py tests/unit/scheduler/test_cursor_evidence_corrections.py tests/unit/scheduler/test_review_budget_extend.py tests/integration/test_phase20_8_sequence_review_retry.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Use isolated Python 3.11+ `.venv`/`uv`; verify local CLI help. Map C-01–C-04 to
production callers and tests; report exact executed, failed and unexecuted checks.
Missing inherited budget, B continuity, record integrity or required coverage
is incomplete scope. No live installation/recovery is validation authority.

## Risks Or Recovery Notes

Reviewer continuity, inherited extensions and exact prompt ancestry remained P1
issues in the combined run. This phase makes those a dedicated acceptance flow,
without sequence replacement. It must remain safe across multiple generations,
including compatibility with the accepted initial successor and existing reviewer
recovery. Do not assume original events are present in each successor journal.
Planning does not execute this phase or authorize model calls.

## OpenQuestions

None. Acceptance of 23.1 and 23.2 is required before dependent implementation.

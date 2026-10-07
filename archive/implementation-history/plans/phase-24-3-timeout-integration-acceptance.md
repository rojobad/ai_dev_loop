# Phase 24.3 — Timeout continuity, integration gate and operational acceptance

## Goals

Close end-to-end activity-timeout behavior across initial turns, corrections,
sequences and existing recovery successors. Verify compatibility and the manual
hard-limit policy, finish current documentation and run the separate final
pipeline/integration gate.

## Non-Goals

Reimplementing the runner/retry loop, broadening recovery eligibility, adding
aggregate token budgets or performing real workstation deployment.

## Scope

Production-path integration/compatibility tests and necessary fixes to accepted
24.1/24.2 behavior, timeout reason/next-action projections, current docs/rules,
Phase 24 acceptance findings, packaging and one final repository-wide gate.

## Out of Scope

No live run recovery, installations/model calls, timer/linger/bus changes,
push/PR/merge, destructive Git or artifact cleanup, new recovery commands,
planning/reviewer skill edits, or root YAML activation without explicit operator
scope. Preserve the pre-existing incident diagnosis and historical plans.

## Required Context

Read [the overview](phase-24-cursor-activity-timeouts.md),
[24.1](phase-24-1-timeout-policy-contracts.md),
[24.2](phase-24-2-activity-aware-execution.md), their accepted findings,
`AGENTS.md` and current CLI/configuration/state/observability/troubleshooting/
privacy/integration API docs. Verify A-01–A-03 and T-01–T-04 against actual
production code/tests, including supported admission and zero automatic retry
after an authenticated hard timeout. No dependency may be inferred from prose
or a partially accepted patch.

Trace scheduler `sequence_prepare.py`, `sequence_start.py`,
`sequence_materializer.py`, checkpoint/handoff services, `cursor_timeout_retry.py`,
`cursor_initial_recovery.py`, `cursor_correction_recovery.py`,
`sequence_cursor_recovery.py`, `cursor_recovery_evidence.py`,
`cursor_budget_carry.py`, `review_recovery.py`, `review_budget.py`, public
contracts/status/history and `integration_api/` projections. Reuse actual accepted
Phase 23 same-chat/B/budget and same-ordinal recovery contracts. Inspect genuine
pre-24 fixtures and findings evidence before extending compatibility tests.

## Cursor Rules And Skills

Follow `AGENTS.md` and the eight `.cursor/rules/` files listed in the overview:
governance, orchestrator, state/schema, loop/recovery, abort, Codex review,
docs/acceptance and global integrations. Read
`.agents/skills/create-cursor-plan/SKILL.md`; respect the separate read-only
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`. For documentation,
apply `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md` within
current user scope and current commands, not its retired historical examples.
No `.cursor/skills/` exists. Keep planning/reviewer skills unchanged.

## Architecture Guardrails

- Preserve exact frozen thresholds, prompts, reviewer identity and review-budget
  authority across existing retry/recovery flows. No re-resolve of current YAML.
- Same-run hard manual waits do not become blocked sequence leaves requiring
  `--force`; future phases remain unmaterialized until accepted completion.
- Existing Phase 23 forced non-timeout recovery remains eligible only through
  its authenticated boundary. A hard timeout does not bypass that policy or
  require a new chat/reviewer/successor.
- Keep reservations, fenced effects and process/checkpoint holds during abort
  or uncertainty, and test eventual convergence when resolution becomes known.
- Public CLI and integration summary fields may expose bounded reason/counters/
  artifact references, never event text or full session IDs. Explicit sensitive
  content reads keep their existing authentication/pagination contract.
- Root YAML/planning/reviewer skill edits are control-plane changes: if explicitly
  added to scope, final manual acceptance is required beyond automated review.

## Implementation Plan

**I-01 — Initial/correction and review continuity.** Drive full production
submit/start/tick flows with activity policy, fake Cursor and fake Codex. Prove
soft survival -> successful initial implementation -> staging -> first B, and
findings -> correction hard timeout -> idle manual wait -> manual retry -> exact
B resume -> accepted result. Include an explicitly extended review ceiling to
prove failed process attempts do not spend/reset review budget. Assert unchanged
raw fix/envelope and policy, preserved partial staged/unstaged files, and no
premature stage/commit while waiting.

**I-02 — Sequences and accepted recovery successors.** Prepare a two-or-more
phase activity sequence using the newly supported manifest. A non-final phase
with hard timeout must keep the same run/ordinal/reservation, without checkpoint
or successor materialization, until explicit no-flag retry succeeds. Ordinary
accepted checkpoint/handoff then runs once and final changes remain staged.
Abort while waiting cancels future phases and releases ownership only after
existing holds resolve. Drive eligible non-timeout initial/correction forced
recovery (standalone and current sequence leaf) through existing Phase 23 CLI
publication/adoption with adaptive frozen inputs; successors retain thresholds
and later obey hard manual waits. Test activity-policy review-recovery context
copy where that real path is affected, without expanding its eligibility.

**I-03 — Genuine compatibility and public diagnostics.** Old submitted runs,
prepared sequences, invocation/completion artifacts and retry events retain
fixed-policy behavior with their original reason-less forms. Use accepted
pre-24 writers/fixtures, not newly generated objects with fields removed. New
activity results missing/corrupt reason cannot enter the historical fallback.
Verify status/history/timeline and integration inspect/report summaries expose
the same safe reason and manual command, preserve read-only behavior, and omit
transcript content. Fix only mismatches proven on these supported paths.

**I-04 — Operational docs and final gate.** Finish Spanish CLI/configuration,
state/observability/troubleshooting and relevant integration API docs. Show the
120/20/360 opt-in block and complete CLI triplet, legacy override precedence,
monotonic activity definition, long silent-tool tradeoff, per-attempt hard bound,
inactivity retries and explicit hard manual continuation/abort. Explain outer
systemd grace and uncertainty; do not claim this repairs the control-bus incident.
Keep new-run examples on the central scheduler. Record contract results,
remaining operator rollout, build/package validation and final integration-gate
results in new findings. No automatic installation or activation is implied.

Changed continuity boundary: authenticated failed/successful attempt -> same-run
retry or existing authenticated successor -> exact review/checkpoint progression.
Each durable owner relation retains frozen policy. Restart/duplicate command
cannot dispatch twice; abort blocks new effects and future phases. Once manual
continuation succeeds or abort reconciles, the supported workflow must finish,
not merely retain ownership indefinitely. Runtime checkpoint commits retain
their existing explicit sequence-start authority, without additional Git gates.

## Testing Criteria

Add `tests/integration/test_phase24_3_timeout_continuity.py` and
`tests/unit/scheduler/test_phase24_3_timeout_compatibility.py`, extending relevant
integration summary tests and genuine historical fixtures. Use real CLI/service
entry points with fake agent executables/backends, injected/accelerated time,
native temporary HOME/XDG/repositories and deterministic restart/abort barriers.

| Contract | Independent acceptance evidence |
| --- | --- |
| I-01 | Full successful initial and correction flows; same chat/B/fix/policy, exact next review count and submitted vs extended ceiling after manual retry. Wait retains partial work and produces no stage/commit/new reviewer. |
| I-02 | Activity sequence waits at original ordinal after hard; repeated ticks/restart do nothing; real manual retry yields exactly one checkpoint and next phase, then final staged output. Abort and uncertain cleanup retain correct holds and eventually finish. Actual Phase 23 initial/correction/sequence recovery successors preserve policy and subsequently enforce hard manual wait. |
| I-03 | Genuine pre-24 fixed run/sequence/retry fixtures validate and continue without reinterpretation; corrupted new reason rejects. CLI/integration summaries show safe matching reason/command and no content sentinel; inspection does not mutate state/config/artifacts. |
| I-04 | Exact CLI help and documented examples agree with implemented inputs/transitions. Focused contracts, strict docs, static checks, package build and one final full-suite gate have recorded outcomes. No real model/service/operator action occurs in validation. |

The motivating regression should fail against the old fixed cutoff and pass
through the actual new runner. No helper may supply missing policy copying,
manual authorization, reviewer continuity or production reservation guards.

## Validation

First run mandatory focused contracts and directly affected regressions:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/integration/test_phase24_3_timeout_continuity.py tests/unit/scheduler/test_phase24_3_timeout_compatibility.py tests/integration/test_cursor_timeout_retry.py tests/integration/test_phase23_1_cursor_recovery_check.py tests/unit/scheduler/test_phase23_2_initial_recovery.py tests/unit/scheduler/test_phase23_3_budget_ancestry.py tests/unit/scheduler/test_phase23_4_sequence_recovery.py tests/integration/test_phase20_3_sequence_handoff.py tests/unit/scheduler/test_review_budget_extend.py
```

Then run the single separate repository-wide pipeline/integration gate specified
in the overview, with static checks, strict docs and build. Do not rerun the
entire suite on each correction/review: rerun failed and affected selections and
broaden only to resolve a concrete remaining risk; report whether changes after
the gate leave its evidence outdated. Add affected integration-summary test node
IDs based on actual projection changes.

Record I-01–I-04 and inherited A/T contract closure, exact executed/failed/
unexecuted commands, package contents and any incomplete gate in
`archive/implementation-history/findings/phase-24-3-timeout-integration-acceptance-findings.md`.
Use Python 3.11+ isolated `.venv`/`uv`. Missing mandatory behavior or coverage is
incomplete scope; do not label it residual risk or claim full validation.

## Risks Or Recovery Notes

The execution sequence was frozen under the old runtime and remains fixed-timeout;
new test sequences independently exercise activity policy. Adoption for future
real runs is explicit operator configuration, not retroactive migration. A manual
retry can spend another six hours; docs must state that it grants another attempt
without an aggregate spending guarantee. Final phase acceptance leaves changes
staged and does not authorize deployment, push, PR creation or merge.

## OpenQuestions

None for implementation. Root YAML activation remains separate optional operator
scope unless explicitly approved before this plan is frozen.

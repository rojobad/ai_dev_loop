# Phase 25.4 — Provider token usage and historical queries

## Goals

Capture and expose usage for Cursor SDK and Codex CLI attempts, with native
categories, deduplication and explicit coverage. Preserve historical status,
history, sequence/review/output inspection without requiring old execution tools.

## Non-Goals

Billing/credit/USD calculation, per-tool/per-internal-call metrics, token budgets,
new model calls, inferred historical Cursor counters or changed review decisions.

## Scope

Versioned usage observations/aggregate DTOs and artifacts, Cursor/Codex ingestion,
read-only scheduler usage commands, run/sequence ancestry aggregation, historical
fixtures/readers, compatible inspection projections, docs and focused tests.

## Out of Scope

Root YAML, SDK execution policy changes, Codex SDK migration, reviewer identity,
old record rewrites, transcripts/billing APIs, live ledger/agent/timer/installation
actions, Phase 24 and planning/review skill edits.

## Required Context

Read [the overview](phase-25-cursor-sdk-and-token-usage.md), accepted
[25.3](phase-25-3-sdk-continuity-and-recovery.md) and
[POC token/history evidence](../findings/phase-25-sdk-poc/README.md). Verify
R-01–R-04, W-01–W-04 and their historical readers. Inspect scheduler Cursor/Codex
attempt runners, outcome ingestion/evidence auth, artifact paths, ledger event/
attempt identity, recovery ancestry/sequence phase-run lineage and public
status/history/timeline services. Inspect `integration_api/` run/sequence/review/
process-output readers, models/version/schemas and CLI renderers.

Codex currently captures JSONL including `turn.completed.usage`; this POC did
not validate its cache semantics. Confirm supported fields against official
Codex documentation and the installed CLI's public event contract/fixtures before
normalization. A repository test fake is not evidence of real field semantics.

## Cursor Rules And Skills

Follow `AGENTS.md` and all eight `.cursor/rules/` in the overview. Read
`.agents/skills/create-cursor-plan/SKILL.md`; preserve it and the separate
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`. No `.cursor/skills/`
exists. Clarify numeric usage versus secret tokens where an affected rule could
otherwise contradict this approved scope; keep secret-retention prohibitions.

## Architecture Guardrails

- Metrics are observations, never review decisions or retry/budget authority.
  No extra inference, transcript reads or provider billing call is allowed.
- One versioned canonical observation per provider+scheduler attempt+native run
  (Cursor), or provider+scheduler Codex attempt+completed-turn ordinal. Evidence
  references include run owner, format, source artifact hash, native offset/turn
  key and normalization version. SDK event/result/replay are evidence for the
  same observation, not separate usage. Conflicting sources are diagnosed, not
  added together or silently overwritten.
- Retain provider-native strict nonnegative integer counters; reject booleans,
  negative/fraction/string-coerced values. Optional reasoning/cache fields are
  nullable unknowns, not fabricated zeros. Unknown extra provider fields may be
  retained in raw evidence but cannot acquire summation semantics automatically.
- Common normalized categories: input_total, input_uncached, cache_read,
  cache_write, output_total, reasoning_output and total_tokens. Cursor total in
  the POC is input+cache_read+cache_write+output; reasoning is an output subset.
  Codex total is input+output, cached_input is an input subset, uncached input
  is input-cached_input when both are valid. Codex cache_write stays unknown
  unless the provider supplies it. Optional missing category data does not
  invalidate a supplied complete total; publish category coverage separately.
- Do not impose Cursor's input semantics on Codex or sum reasoning again.
  Validate known provider relationships with independent numeric examples. If
  official evidence contradicts this normalization, stop dependent normalization
  and report it rather than silently changing accounting.
- Observation availability is known/partial/unknown with safe reason. Preserve
  reported counters as a known lower bound when capture is incomplete. Missing
  usage after inference/cancel/crash is unknown, never zero. An authenticated
  preflight failure with no inference has a separate not_applicable classification;
  no SDK usage observation is fabricated for agent creation or catalog reads.

## Implementation Plan

**U-01 — Versioned capture and idempotent publication.** Define typed native and
normalized observation schemas under existing protected per-attempt artifacts.
Integrate actual Cursor outcome/native replay and Codex JSONL outcome ingestion.
Record observations only from authenticated evidence for the correct attempt.
Interrupted/truncated captures remain partial/unknown, with capture flags and
attempt status. Filesystem/ledger observation publication uses durable fences
and idempotent reconciliation; re-observation cannot overwrite or double count.
Malformed metric data records safe unavailable diagnostics and does not turn an
otherwise accepted implementation/review into a failed decision.

**U-02 — Coverage-aware aggregation.** Aggregate actual dispatched inference
attempts, including corrections, timeout/capacity/manual retries and failed
attempts with known metrics. Repeated reads and recovered envelopes are not new
attempts. Per provider expose known_total_tokens, inference_attempts,
known_attempts, partial_attempts, unknown_attempts, category totals/coverage and
aggregate coverage (`complete`, `partial`, `unknown`, or `not_applicable` when
no inference was dispatched). A zero-valued reported observation is known zero.
If every inferred attempt is unknown, known_total_tokens is null, not zero.

Run detail includes its direct attempts; additionally expose an explicitly
labelled recovery-lineage total over authenticated ancestors once each. Sequence
phase/sequence totals follow authenticated phase-run lineage, including replaced
leaves, excluding unadopted/unrelated successors and avoiding double inclusion
through both ancestor and phase-run edges. Preserve a provider breakdown before
an optional combined known-total summary. Add no billing-cost field.

**U-03 — Read-only query surface.** Add `scheduler usage RUN_ID --output human|json`
and `scheduler sequence usage SEQUENCE_ID --output human|json`, following existing
command/render conventions. Display explicit known total and coverage/reasons,
with direct-versus-lineage scope labelled. These are proposed commands to implement
in this slice, not currently available baseline commands. Queries use validated
ledger/artifacts in read-only transactions, never launch SDK/CLI/auth or advance
state. References and safe summaries exclude raw content/full identities.
Add usage summaries to existing run/sequence inspection through the documented
versioned/additive Integration API contract; preserve old consumers' schemas and
capability/version conventions instead of unversioned wire fields.

**U-04 — Genuine historical inspection.** Run status/list/history/timeline and
integration run/sequence inspect, phase-run lineage, review and explicit output
reads over authentic pre-25 fixtures with no SDK extra, CLI, credentials, native
store or bridge. Preserve reader compatibility introduced in earlier slices.
Absent historical Cursor counters produce unknown coverage. Codex events may be
parsed read-only for metrics only if the original attempt/source can be
authenticated; any derived result remains a projection, with original bytes/
hashes intact. Do not backfill historical state or rewrite missing artifacts.
Retain established availability/redaction behavior for missing content.

Boundary: authenticated attempt evidence -> immutable versioned observation ->
durable deduplicated publication -> read-only aggregation. Interrupted publication
reconciles the same observation; replay is not new consumption. Historical
projection neither migrates the database nor requires the provider runtime.

## Testing Criteria

Add `tests/unit/scheduler/test_phase25_4_token_usage.py`,
`tests/integration/test_phase25_4_token_usage.py` and
`tests/unit/integration_api/test_phase25_4_historical_usage.py`. Use synthetic SDK/
Codex native events, genuine historical ledger/artifact fixtures, mock time and
fake provider workers; no external calls. Independent expected values include
the POC example: 4820+2560+0+139=7519, with 118 reasoning already in output.
Use a separate Codex example with input=100, cached=60, output=10 yielding total
110 and uncached=40, not 170. Confirm Codex semantics against primary evidence.

| Contract | Production-path acceptance |
| --- | --- |
| U-01 | Real Cursor/Codex ingestion publishes one observation despite event/result/replay and crash replay; unknown/null/zero/malformed/partial flags are distinct; metrics never change accepted review outcome. |
| U-02 | Initial+correction+retry+failed/successor fixture yields independent direct/lineage/sequence totals once per native attempt; missing attempts yield correct lower-bound coverage and provider category semantics. |
| U-03 | Actual usage CLI and inspection API provide human/JSON known totals and coverage, remain read-only, redact sentinels and preserve old consumer envelopes; no auth/runtime dependency. |
| U-04 | Historical status/list/history/timeline/inspection/content commands succeed without old tools; fixture bytes/hashes unchanged and old Cursor usage unknown; authenticated old Codex usage is projected without backfill. |

Race/publication tests use production fencing and independent expected totals,
not sums computed by calling the same normalizer being tested. Test conflicting
source observations, multiple completed Codex turns, partial stream, null category,
known zero, unadopted successor, shared ancestry and repeated sequence query.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase25_4_token_usage.py tests/integration/test_phase25_4_token_usage.py tests/unit/integration_api/test_phase25_4_historical_usage.py tests/unit/scheduler/test_schema_readonly_historical.py tests/integration/test_phase21_2_run_inspection.py tests/integration/test_phase21_3_sequence_inspection.py tests/integration/test_phase21_4_review_inspection.py tests/unit/integration_api/test_contract.py tests/unit/integration_api/test_output_projection.py
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Report U-01–U-04, normalization evidence/version, observation/aggregate schema,
fixture provenance, query examples and exact results in
`archive/implementation-history/findings/phase-25-4-token-usage-findings.md`.
Full-suite gate belongs to 25.5. Missing historical-reader or accounting coverage
is incomplete scope, even when the POC checks pass.

## Risks Or Recovery Notes

Cancelled/crashed usage can remain unknown indefinitely. Don't promise complete
billing or calculate USD from an undifferentiated token total. Preserve raw
provider categories for audit and future normalization changes, without mutating
old evidence. A failed metric projection must remain distinguishable from a
failed agent/review decision and must not trigger another agent call.

## OpenQuestions

None. Provider normalization must be supported by primary evidence; report any
contract conflict before implementing a different accounting rule.

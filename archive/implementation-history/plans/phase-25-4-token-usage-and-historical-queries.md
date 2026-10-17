# Phase 25.4 — Provider token usage and historical queries

## Goals

Capture and expose usage for Cursor SDK implementation and both Codex CLI
roles (implementer and reviewer), with native categories, role/turn attribution,
deduplication and explicit coverage. Preserve historical status,
history, sequence/review/output inspection without requiring old execution tools.

## Non-Goals

Billing/credit/USD calculation, per-tool/per-internal-call metrics, token budgets,
new model calls, inferred historical Cursor counters or changed review decisions.

## Scope

Versioned usage observations/aggregate DTOs and artifacts, Cursor/Codex ingestion,
read-only scheduler usage commands, run/sequence ancestry aggregation, historical
fixtures/readers, compatible inspection projections, Dashboard read regressions,
docs and focused tests. U-05 owns preservation of current Dashboard semantics.

## Out of Scope

Root YAML, SDK execution policy changes, Codex SDK migration, reviewer identity,
old record rewrites, transcripts/billing APIs, live ledger/agent/timer/installation
actions, Phase 24, new Dashboard token charts/UI and planning/review skill edits.

## Required Context

Use the replanned overview baseline (`558b799`, implemented Phases 26–28),
not the original Phase 23-only checkout. Preserve Codex implementation/recovery,
role-specific capacity and reviewer identities, manifest v3 short names, SQLite
schema-17 presentation metadata and Integration API 1.11/Dashboard reads. The
installed driver executes this slice with Codex `gpt-5.6-sol` / `xhigh`; project
Cursor SDK model settings describe the feature being built, not this execution.

Read [the overview](phase-25-cursor-sdk-and-token-usage.md), accepted
[25.3](phase-25-3-sdk-continuity-and-recovery.md) and
[POC token/history evidence](../findings/phase-25-sdk-poc/README.md). Verify
R-01–R-04, W-01–W-04 and their historical readers. Inspect scheduler Cursor/Codex
attempt runners, outcome ingestion/evidence auth, artifact paths, ledger event/
attempt identity, recovery ancestry/sequence phase-run lineage and public
status/history/timeline services. Inspect `integration_api/` run/sequence/review/
process-output readers, models/version/schemas and CLI renderers. Include
`codex_implementer_attempt_runner.py` and its authenticated role-specific events/
metadata, `codex_attempt_runner.py`, and `application/codex_evidence.py`. Read
`dashboard/application/{queries,ports}.py`, `dashboard/domain/metrics.py`,
`dashboard/adapters/sqlite_read.py`, `integration_api/dashboard_service.py`,
process-output auth/resolution, and strict frontend parsers. Current API is 1.11.

Codex currently captures JSONL including `turn.completed.usage`; this POC did
not validate its cache semantics. Confirm supported fields against official
Codex documentation and the installed CLI's public event contract/fixtures before
normalization. A repository test fake is not evidence of real field semantics.

## Cursor Rules And Skills

Follow `AGENTS.md` and all ten `.cursor/rules/` in the overview. Read
`.agents/skills/create-cursor-plan/SKILL.md`; preserve it and the separate
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`. No `.cursor/skills/`
exists. Clarify numeric usage versus secret tokens where an affected rule could
otherwise contradict this approved scope; keep secret-retention prohibitions.

## Architecture Guardrails

- Metrics are observations, never review decisions or retry/budget authority.
  No extra inference, transcript reads or provider billing call is allowed.
- Authenticate role, turn kind and review iteration from attempt/effect/invocation
  evidence. Persist `implementer` or `reviewer`, provider and initial/correction/
  review stage independently of the model name. Historical role-absent Codex
  review effects keep reviewer semantics; never guess from free text. Capacity
  probes, preflight and SDK reconciliation-only effects are not inference.
- One versioned canonical observation per provider+role+scheduler attempt+native run
  (Cursor), or provider+role+scheduler Codex attempt+completed-turn ordinal. Evidence
  references include run owner, format, source artifact hash, native offset/turn
  key and normalization version. SDK event/result/replay are evidence for the
  same observation, not separate usage. Conflicting sources are diagnosed, not
  added together or silently overwritten. A known SDK native run recovered by a
  different reconciliation attempt belongs to the original dispatch observation;
  that recovery must not create a second consumption record under a new attempt.
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
Integrate Cursor outcome/native replay and both Codex implementer/reviewer
JSONL outcome ingestion, preserving their distinct artifact auth paths and roles.
Record observations only from authenticated evidence for the correct attempt.
Interrupted/truncated captures remain partial/unknown, with capture flags and
attempt status. Filesystem/ledger observation publication uses durable fences
and idempotent reconciliation; re-observation cannot overwrite or double count.
Malformed metric data records safe unavailable diagnostics and does not turn an
otherwise accepted implementation/review into a failed decision.

**U-02 — Coverage-aware aggregation.** Aggregate actual dispatched inference
attempts, including corrections, timeout/capacity/manual retries and failed
attempts with known metrics. Repeated reads and recovered envelopes are not new
attempts. Per provider and role/stage expose known_total_tokens, inference_attempts,
known_attempts, partial_attempts, unknown_attempts, category totals/coverage and
aggregate coverage (`complete`, `partial`, `unknown`, or `not_applicable` when
no inference was dispatched). A zero-valued reported observation is known zero.
If every inferred attempt is unknown, known_total_tokens is null, not zero.

Run detail includes its direct attempts; additionally expose an explicitly
labelled recovery-lineage total over authenticated ancestors once each. Sequence
phase/sequence totals follow authenticated phase-run lineage, including replaced
leaves, excluding unadopted/unrelated successors and avoiding double inclusion
through both ancestor and phase-run edges. Include Phase 27 Codex implementer
recovery ancestry as well as Cursor/reviewer successors; do not lose spent usage
from adopted replaced leaves. Preserve provider+role/stage breakdowns before an
optional combined known-total summary; two Codex roles are distinct contributions,
not two names for the same observation. Add no billing-cost field.

**U-03 — Read-only query surface.** Add `scheduler usage RUN_ID --output text|json`
and `scheduler sequence usage SEQUENCE_ID --output text|json`, following existing
command/render conventions. Display explicit known total and coverage/reasons,
with direct-versus-lineage scope labelled. These are proposed commands to implement
in this slice, not currently available baseline commands. Queries use validated
ledger/artifacts in read-only transactions, never launch SDK/CLI/auth or advance
state. References and safe summaries exclude raw content/full identities.
Keep CLI output spelling consistent with installed `text|json` conventions.
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
Retain established availability/redaction behavior for missing content. Include
genuine Phase 27 Codex implementer and reviewer captures plus Phase 28 names and
v2/v3 definition fixtures, not only old Cursor-only records.

**U-05 — Dashboard and read-side compatibility.** Exercise the public Dashboard
query/HTTP/Integration API and existing browser consumers with new SDK records,
Codex-only records, mixed sequences, recovery lineage and retained history.
SDK implementation effects must count as started execution/process time, never
review rounds; Codex implementer must remain separate from Codex reviewer.
Preserve project/date/IANA-zone filters, names, exact-ID selection/links, paging
(including adaptive runs batches), observation bounds and existing coverage math.
Missing usage cannot make an otherwise readable run disappear or change its
activity totals. New usage fields follow additive capability/version/schema
conventions; current strict browser parsers must still accept existing routes.
No new token panel or activity metric is required. Reads never migrate SQLite,
rename, backfill observations, poll sensitive content or launch a model.

Boundary: authenticated attempt evidence -> immutable versioned observation ->
durable deduplicated publication -> read-only aggregation. Interrupted publication
reconciles the same observation; replay is not new consumption. Historical
projection neither migrates the database nor requires the provider runtime.

## Testing Criteria

Add `tests/unit/scheduler/test_phase25_4_token_usage.py`,
`tests/integration/test_phase25_4_token_usage.py` and
`tests/unit/integration_api/test_phase25_4_historical_usage.py` and
`tests/integration/test_phase25_4_dashboard_compatibility.py`. Use synthetic SDK/
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
| U-04 | Historical status/list/history/timeline/inspection/content commands succeed without old tools; fixture bytes/hashes unchanged and old Cursor usage unknown; authenticated old Codex usage from either role is projected without backfill. |
| U-05 | Dashboard application/HTTP/API reads new and historical providers/lineage with unchanged independent activity totals, filters/paging/names and strict client compatibility; authority bytes are unchanged and missing metrics do not hide runs. |

Race/publication tests use production fencing and independent expected totals,
not sums computed by calling the same normalizer being tested. Test conflicting
source observations, multiple completed Codex turns, partial stream, null category,
known zero, unadopted successor, shared ancestry and repeated sequence query.
Declare an independent both-Codex example: implementer initial=110, correction=20,
reviewer initial=30, second review=40 gives implementer=130, reviewer=70 and
combined=200. A capacity probe contributes no inference; an interrupted inference
without counters makes combined coverage partial while keeping known total 200.
Assert role attribution through real attempt evidence, not test-provided role labels.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase25_4_token_usage.py tests/integration/test_phase25_4_token_usage.py tests/unit/integration_api/test_phase25_4_historical_usage.py tests/unit/scheduler/test_schema_readonly_historical.py tests/integration/test_phase21_2_run_inspection.py tests/integration/test_phase21_3_sequence_inspection.py tests/integration/test_phase21_4_review_inspection.py tests/unit/integration_api/test_contract.py tests/unit/integration_api/test_output_projection.py tests/integration/test_phase25_4_dashboard_compatibility.py tests/unit/dashboard tests/integration/test_phase26_1_dashboard_queries.py tests/integration/test_phase26_2_dashboard_server.py tests/integration/test_phase27_3_inspection.py tests/integration/test_phase28_dashboard_paging.py
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Report U-01–U-05, normalization evidence/version, observation/aggregate schema,
fixture provenance, query examples and exact results in
`archive/implementation-history/findings/phase-25-4-token-usage-findings.md`.
If public DTOs/assets change, run Dashboard typecheck and focused parser/
modal/data-source Vitest tests (`npm run typecheck`; `npm test -- --run` with
affected test paths), and regenerate packaged assets through the existing build
script. Do not edit generated dist without matching source/build provenance.
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

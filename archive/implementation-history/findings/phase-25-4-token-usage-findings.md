# Phase 25.4 findings — provider token usage and historical queries

Date: 2026-10-17; correction validation updated 2026-10-18
(America/La_Paz)

Implementer: Codex implementation role. Reviewer B remains separate and is not
created or reused by this implementation. This work did not invoke a real model,
change credentials/timers/live scheduler state, install software, run cleanup, or
perform external/destructive Git actions.

## Prerequisites and authority

- Reapplication started from `1aedce3`, which is accepted Phase 25.3 checkpoint
  `50bfed0` plus the plan-owner deferral and re-preparation documentation commits,
  on top of accepted Phase 25.1/25.2 and baseline `558b799` containing accepted
  Phases 26–28. Phase 24 was not used.
- The worktree was clean before implementation. Schema 18, run contexts v7/v8,
  manifest v4, Integration API 1.11 and the Phase 28 presentation metadata were
  present. Accepted Phase 25.1–25.3 findings supplied R-01–R-04 and W-01–W-04.
- The Phase 25 overview, Phase 25.3 plan, preserved POC index, `AGENTS.md`, all ten
  listed `.cursor/rules/*.mdc`, and the requested implementation/governance skills
  were read. No `.cursor/skills/` directory was assumed.
- `OpenQuestions`: none in the approved Phase 25.4 plan. The checkout still has no
  genuine pre-25 scheduler ledger/artifact fixture with token-bearing Codex captures
  for both implementer and reviewer roles, and Node/npm is still unavailable. The
  plan-owner decision of 2026-10-18 accepts these exact gaps as deferred residual
  risks D-01/D-02; neither is fabricated or treated as a mandatory blocker.
- The preserved patch SHA-256 was verified as
  `2a5e703cd52eff2445a00112de4bdbf980df97fd2c54bbc93bff1494653e7632` and
  `git apply --check` passed. The managed sandbox makes `.git` read-only, so the
  required `git apply --index` failed before modification while creating
  `.git/index.lock`; the same verified patch was then applied to the working tree.
  Default-index staging remains an operator/scheduler action outside this sandbox.
- The implementation preserved provider/model/timeouts, role-specific capacity and
  recovery behavior, reviewer/implementer identities, Git authority, short names and
  frozen settings. Token observations never authorize a retry, budget, review result,
  checkpoint, commit, or provider switch.

## Normalization evidence and version

Normalization version: `provider-usage-v1`; observation schema version: 1;
aggregate/report schema version: 1; Integration API: 1.12 with additive capability
`tokenUsage`.

Primary evidence checked before implementation:

- Cursor Agent SDK Python reference documents `input_tokens`, `output_tokens`,
  `cache_read_tokens`, `cache_write_tokens`, `total_tokens` and optional
  `reasoning_tokens`; total includes the four non-reasoning categories and reasoning
  is already a subset of output:
  <https://cursor.com/docs/sdk/python>
- Installed `codex-cli 0.161.0` was inspected read-only. Its matching public source
  defines completed-turn usage as `input_tokens`, `cached_input_tokens`,
  `cache_write_input_tokens`, `output_tokens` and `reasoning_output_tokens`:
  <https://raw.githubusercontent.com/openai/codex/rust-v0.161.0/codex-rs/exec/src/exec_events.rs>
  and
  <https://raw.githubusercontent.com/openai/codex/rust-v0.161.0/codex-rs/protocol/src/protocol.rs>.

The implemented arithmetic is therefore:

- Cursor: `4820 + 2560 + 0 + 139 = 7519`; reasoning `118` remains inside output
  `139`, and normalized input total is `4820 + 2560 + 0 = 7380`.
- Codex: input `100`, cached input `60`, output `10` gives total `110` and uncached
  input `40`, not `170`. Missing Codex cache-write remains `null`.
- Strict native counters reject booleans, negatives, fractions, string coercion and
  invalid subset/total relationships. Unknown provider additions remain in the
  immutable raw provider artifact but do not acquire aggregation semantics.

## Schemas and publication boundary

- `UsageObservationV1` is an immutable strict model keyed by
  provider + role + logical scheduler attempt + native Cursor run, or by the Codex
  completed-turn ordinal. Cursor event offsets remain immutable contribution evidence
  inside that native-run observation; they are not separate consumption identities.
  The model records logical/publisher attempt identities, source dispatch, source
  path/hash/format, capture completeness, native counters and normalized counters.
- Observations use write-once protected paths
  `usage/observations/<prefix>/<observation-id>.json`. Safe diagnostics use separate
  write-once paths. Publication is trusted only through the existing authenticated
  attempt completion envelope/outcome fence; orphan files have no read authority.
- Semantic replay of the same identity/counters is idempotent even when a Cursor SDK
  reconciliation is the publisher. A disagreement is diagnosed and is never
  overwritten or summed. Reconciliation assigns the observation to the original
  source attempt/dispatch, not the reconciliation attempt.
- `UsageScopeReport` and its typed breakdown/category DTOs expose direct,
  recovery-lineage and sequence scopes. The shared public JSON schema is
  `integration-api-usage-shared-v1.json`; run/sequence inspect schemas reference it.
- Coverage is `complete`, `partial`, `unknown`, or `not_applicable`. A reported zero
  is known zero; missing inference counters are unknown; partial known counters stay
  a lower bound. If all inferred attempts are unknown, `knownTotalTokens` is `null`.

## Contract findings

### U-01 — versioned capture and idempotent publication: implemented

Production callers:

- `scheduler/cursor_attempt_runner.py::_sdk_capture_payload` for Cursor SDK stream
  events, terminal results and exact native replay.
- `scheduler/codex_implementer_attempt_runner.py::_execute` for authenticated
  implementer initial/correction JSONL.
- `scheduler/codex_attempt_runner.py::_run_codex_review` for authenticated reviewer
  JSONL; reviewer outcomes now bind `events_sha256`.
- `application/token_usage_capture.py` supplies strict normalization, completed-turn
  ordinal capture, immutable publication, replay verification and safe diagnostics.

Independent tests:

- `tests/unit/scheduler/test_phase25_4_token_usage.py`: independent Cursor/Codex
  arithmetic, strict invalid types/relationships, known zero, partial capture,
  multiple Codex turns, conflict and replay behavior.
- `tests/integration/test_phase25_4_token_usage.py`: real fake-backed Cursor SDK,
  Codex implementer and reviewer runners. Malformed review usage leaves an accepted
  review/run completed and appears only as a safe diagnostic.

Result: each Codex completed turn and each native Cursor run has one canonical
observation. Cursor event offsets describe distinct per-turn contributions within the
run; cumulative/replayed evidence confirms that run identity without adding it again.
Metric failures do not change agent exit/review semantics.

### U-02 — coverage-aware aggregation: implemented

Production callers:

- `TokenUsageReadService.run_report` and `.sequence_report` authenticate attempts,
  deduplicate observation IDs, preserve provider/role/stage breakdowns, recursively
  include authenticated run-recovery ancestors once, and use only
  `scheduler_sequence_run_attempts` for authoritative sequence membership.
- Capacity probes, create/preflight and reconciliation-only effects are excluded from
  inference counts. An authenticated SDK preflight failure without a native run is
  `not_applicable`, not unknown inference.

Independent tests:

- Both-Codex production workflow: implementer initial `110`, correction `20`, and
  known zero second correction => implementer `130`; reviews `30` and `40` =>
  reviewer `70`; a third interrupted review is unknown. Combined known lower bound
  is exactly `200` with partial coverage.
- Sequence workflow asserts reported run IDs equal the authoritative phase-run
  lineage. A real Phase 27 implementer-recovery replacement retains the blocked
  leaf and adopted successor through their shared ancestry and counts the successor
  contribution once across repeated queries.
- Empty inference scope is `not_applicable` with a null known total.

Result: direct, recovery-lineage and sequence scopes are independently labelled;
native and normalized category totals/coverage are retained without billing fields.

### U-03 — read-only query surface: implemented

Production callers:

- `scheduler usage RUN_ID --output text|json`.
- `scheduler sequence usage SEQUENCE_ID --output text|json`.
- Integration run/sequence inspect emit typed additive `tokenUsage`; API 1.12 and
  `capabilities.tokenUsage=true` advertise the field.

Independent tests:

- CLI queries run after `PATH` is replaced with a nonexistent provider-runtime path.
- Schema-11 historical ledger bytes and `user_version` are identical before/after
  actual CLI reads; missing databases remain absent. Current ledger and protected-
  artifact byte maps are also identical before/after reads.
- JSON schemas validate current responses and old major-1 fixtures remain accepted
  without `tokenUsage`.

Example results: direct both-Codex known total `140` with complete coverage;
sequence known total `170` with partial coverage because the Cursor CLI attempt has
no provider counter. Text output explicitly labels `direct`, `recovery_lineage` and
provider/role/stage.

### U-04 — genuine historical inspection: implemented; D-01 deferred

Production callers:

- The existing status/list/history/timeline, run/sequence/review/output readers were
  left runtime-independent.
- When no observation artifact exists, `TokenUsageReadService` derives Codex usage
  in memory only if the authenticated completion outcome binds both JSONL path and
  hash. It never writes/backfills. Old Cursor attempts remain unknown.

Demonstrated independent evidence:

- The required historical schema/read-only, Phase 21 inspection and Phase 27
  inspection suites passed in the focused matrix.
- A production-runner fallback removes only additive observation files, then
  projects both authenticated Codex roles from unchanged path/hash-bound JSONL.
  This proves the projection path and byte immutability, but correction review
  correctly rejected it as proof of genuine pre-25 capture compatibility.
- Phase 27 provider-role inspection and Phase 28 naming/paging fixtures passed without
  old provider runtimes.

Deferred evidence: `tests/fixtures/phase21_4_pre_21_4_historical` is a genuine
pre-25 ledger/artifact capture, but its sole reviewer JSONL contains only
`thread.started` and `message`; it has no `turn.completed.usage`. The fixture has no
Codex implementer attempt at all. No other checked-in historical fixture contains a
token-bearing Codex completed turn. Genuine historical reads with absent counters,
the authenticated projection path for both roles, Phase 27 provider/role records and
Phase 28 version/name contracts are covered. Genuine pre-25 token-bearing captures
for both roles remain unproven as D-01, accepted residual risk; they must not be
generated with Phase 25.4 code.

### U-05 — Dashboard and read-side compatibility: implemented; D-02 deferred

Production callers:

- Dashboard queries/HTTP continue to use their independent ledger metric rules.
  No Dashboard route, panel, chart, metric or generated asset was added.
- Current browser parsers select known run/sequence inspect fields and tolerate the
  additive `tokenUsage` member.

Independent tests:

- `tests/integration/test_phase25_4_dashboard_compatibility.py` confirms Codex
  implementer contributes process time, reviewer contributes one review round,
  the run remains visible, and reads leave SQLite byte-identical.
- Existing Dashboard unit, query, server and Phase 28 adaptive paging suites all pass.
- A recovered SDK run is read through the production Dashboard HTTP dispatcher with
  exact `7519` lower-bound usage while database/artifact hashes remain unchanged;
  the existing live HTTP server contract also validates additive run/sequence
  `tokenUsage` envelopes for a mixed-provider sequence.

Result: backend compatibility is green. Frontend typecheck/focused Vitest could not
start in the aborted run because this workstation has neither `npm` nor any
Node-compatible runtime. The plan-owner decision accepts that unexecuted frontend
gate as D-02 rather than authorizing toolchain installation.

## Correction findings F-01–F-08

### F-01 — fixed

Production path: `scheduler_usage` and `scheduler_sequence_usage` now pass
`SqliteSchedulerStore.open_readonly(...)` to `TokenUsageReadService`; neither
command constructs the bootstrapping/migrating store.

Independent tests: actual Typer CLI invocations run against the genuine schema-11
historical database and compare its complete bytes and `PRAGMA user_version` before
and after both run and sequence usage commands. Separate missing-database CLI cases
assert the database and state-directory contents are not created or changed.
Transient SQLite `*.sqlite3-shm`/`*.sqlite3-wal` sidecars left beside the checked-in
fixture by external inspection are now ignored so orchestration cannot accidentally
stage them; the files were neither indexed nor deleted during this correction.

### F-02 — fixed

Production path: `_collect` reads inference rows before resolving artifacts. Missing
artifact root/container/run directories produce `usage_evidence_not_recorded` and
unattributed unknown attempts. Existing-but-unsafe roots produce the distinct
`usage_evidence_integrity_error`. Neither case fabricates a breakdown, zero total,
or complete coverage.

Independent tests: production Integration API run and sequence inspection succeed
with nonexistent artifact roots, retain their ordinary state data, and expose null
known totals/unknown coverage. A non-directory artifact root exercises the integrity
diagnostic separately.

### F-03 — fixed

Production path: `CursorSdkAdapter._terminal_result` retains original counter types
and missing values. Strict normalization happens only at usage capture; malformed
usage publishes a safe diagnostic and never changes the terminal result, accepted
review, or run state. SDK metadata retains only allowlisted scalar usage keys and
does not persist malformed string values.

Independent fake-SDK production cases cover boolean, string, fractional, negative,
null, missing-required-field and valid-zero counters. All seven runs complete with
the same accepted review decision; the first six are unknown with
`invalid_cursor_usage`, while zero is known zero with complete Cursor coverage.

### F-04 — fixed

Production path: usage outcomes now bind completed/observed contribution counts and
attempt-level completeness. Live and historical Codex readers mark a whole attempt
partial when any completed turn is missing/malformed or the stream is incomplete;
known surviving counters remain lower bounds and category coverage is also partial.
The read service now also reconciles every authenticated publication binding with
what was actually loaded. A missing or invalid observation forces partial coverage.
For Codex, authenticated path/hash-bound JSONL is projected and merged by canonical
completed-turn identity even when another observation survived, recovering any
available missing counters without writing a replacement artifact.

Independent tests: a live implementer attempt with one `110`-token turn and one
missing contribution remains exactly `110`, partial at attempt and category levels.
The authenticated historical projection gives the same result and reports the
source omission. A separate interrupted-stream test retains the reported `110`
tokens with partial availability. A production sequence attempt with authenticated
turns `110 + 20` then loses only the second published observation: run, recovery-
lineage, sequence, CLI and Integration API inspection all recover total `130` from
the unchanged authenticated JSONL while retaining partial attempt and total-category
coverage plus `usage_observation_integrity_error`. Database bytes and the remaining
artifact hash map stay unchanged across those reads.

The previous correction accounted for missing contributions inside JSONL but treated
the capture-time completeness flag as permanent. It neither compared publication
bindings with successfully loaded files nor attempted the historical merge when one
observation had already survived.

### F-05 — fixed, including reopened deadline path

Production callers: `cursor_attempt_runner.py::_sdk_capture_payload` forwards stream
and terminal/replay evidence;
`token_usage_capture.py::capture_cursor_usage_evidence_safely` reconciles and
publishes it; `TokenUsageReadService` supplies run, lineage and sequence totals to
the usage CLI and Integration API inspection callers. The SDK envelope sanitizer
preserves only exact allowlisted numeric usage names while continuing to redact broad
token/secret/API-key names and string values. The canonical consumption identity is
provider + role + logical scheduler attempt + native Cursor run. Distinct event
offsets remain contribution evidence inside that observation and are aggregated,
including equal-valued turns. The aggregate is reconciled with the cumulative
terminal/result value, which confirms the native-run total rather than becoming
another observation. Replays that switch from event evidence to cumulative-only
evidence bind the same observation ID and verify the already published bytes; they
neither overwrite the earlier protected artifact nor add overlapping consumption.
If one source is malformed while another supplies a valid lower bound, the lower
bound remains partial with a safe diagnostic.

Independent synchronized production test: the fake SDK emits `7519`, the stream
fails, reconciliation replays the same native run, and the run completes. Querying
through both `TokenUsageReadService` and the Dashboard HTTP dispatcher returns
exactly `7519`, one inference attempt and partial coverage. One bound observation is
attributed to the original dispatch. A second valid protected observation published
without an authenticated outcome binding is fenced out and leaves the repeated total
unchanged.

Additional fake-SDK production cases emit `110` then `20` with cumulative result
`130`, and two equal `20` turns with cumulative result `40`. Both complete with exact
totals, one native-run observation retaining both offsets, complete coverage and no
conflict diagnostic. A sparse replay case emits `110` before interruption, emits no
usage event during replay, and reports cumulative `110`; run, recovery-lineage,
manifest-v4 sequence, CLI and Integration API inspection all report one attempt and
exactly `110`. The tests compare SQLite bytes, protected-artifact hashes and the
original observation bytes before/after every read. The owned-process replay case
continues to prove no prompt redispatch or duplicate counting.

The first correction modeled stream and terminal values as interchangeable copies of
one total and deduplicated them by counter equality. It rejected real per-turn
`110 + 20 = 130` evidence and would collapse distinct equal-valued turns. The next
correction retained offsets by turning each event and cumulative evidence into
separate summable observation identities. That fixed multi-turn aggregation but
missed the representation-switch path: an interrupted event total `110` followed by
a cumulative-only replay `110` had no shared identity and was added as `220`. Keeping
offsets as evidence under the native-run identity fixes both cases.

A separate owned-process production test kills the worker after the usage event but
before any attempt result exists. Scheduler reconciliation stops the retained bridge/
tool descendants, replays the same native run without redispatch, publishes one
observation for the original attempt, and returns exactly `7519` once with complete
Cursor-attempt coverage.

The reopened deadline defect was confirmed in
`cursor_attempt_runner.py::_run_sdk_turn_operation`: its
`CursorSdkOperationTimeout` handler published the partial observation but discarded
the returned binding payload. The outer `main` exception path then authenticated an
outcome containing only `_sdk_uncertainty_payload` metadata, so readers correctly
fenced out the otherwise valid observation until reconciliation happened to bind the
same canonical identity. `CursorSdkOperationTimeout` now carries that exact capture
payload through the unwind, and `main` merges it into the authenticated result after
building the uncertainty/journal evidence. This does not scan orphan artifacts or
relax publication authority. The logical source attempt/dispatch and native run ID
remain unchanged, so later reconciliation verifies the same observation rather than
adding it.

Independent production-path regression: a fake SDK emits cumulative usage `100 + 10
= 110` and invokes the installed `SIGALRM` handler. At the exact
`cursor_sdk_reconciliation_requested` receipt—before reconciliation executes—the
usage service, actual CLI, Integration API inspection and Dashboard HTTP inspection
all expose one Cursor inference attempt, total `110`, and partial attempt/category
coverage. After replay and accepted completion, direct and recovery-lineage totals
remain `110`, only one protected observation exists, and exactly one reconciliation
attempt occurred. The earlier F-05 correction missed this because its stream-failure
case returned a capture payload normally; its deadline test observed only after
reconciliation and therefore did not exercise the exception envelope as the sole
publication binding.

The next correction finding reproduced a second deadline during bridge cleanup.
Production callers are `cursor_attempt_runner.py::_sdk_operation_deadline`,
`_run_sdk_turn_operation`, `CursorSdkAdapter.close`, and `main`'s authenticated
timeout completion. The first deadline exception carried the captured binding, but
the repeating signal handler constructed a new exception during a slow
`adapter.close()`; that replacement had an empty payload. The previous test bridge
closed immediately, so the first exception and its instance-local dictionary always
survived. The deadline context now owns one in-memory capture carrier and supplies
the same carrier to every timeout it raises. The stream handler updates it after
publishing the observation, so a later timeout replacing the exception during
bounded cleanup still reaches `main` with the original authenticated binding. The
repeat interval and shutdown/recovery bounds are unchanged, and no artifact scan or
new publication authority was introduced.

The independent production regression emits `100 + 10 = 110`, waits for the real
one-second deadline, and holds fake bridge closure until the unchanged 250-ms repeat
fires. A durable test marker proves the second exception occurred. Before
reconciliation, authenticated direct and recovery-lineage queries, the actual usage
CLI, Integration API inspection and Dashboard HTTP projection all report exactly one
partial Cursor attempt and partial total category with total `110`. Reconciliation
then replays the same native run; the completed run still has one observation and
reports `110` once. The original single-deadline test and all F-08 complete/partial/
unknown status cases remain green.

### F-07 — fixed

Production callers: `cursor_attempt_runner.py::_sdk_capture_payload` and
`token_usage_capture.py::_reconcile_cursor_usage`, followed by the same run/lineage/
sequence query and public inspection surfaces. Reconciliation compares every
required cumulative counter exactly, but treats an absent optional
`reasoning_tokens` category as unknown rather than contradictory. If either source
supplies reasoning and the other omits it, the known value is retained; two supplied
unequal values still conflict. Normalization is rerun from the reconciled native
payload, so total coverage is complete without inventing a zero and reasoning keeps
its own category coverage.

Independent fake-SDK production regression: a stream reports total `110` without
reasoning and its terminal result reports the same required counters plus reasoning
`2`. The completed run reports total `110`, complete Cursor and total-token coverage,
reasoning total `2` with complete normalized/native reasoning-category coverage, and
no `conflicting_cursor_usage_evidence` diagnostic. The unchanged external reviewer
reproduction passes the same production path.

### F-08 — fixed

Production callers: `cursor_attempt_runner.py::_run_sdk_turn_operation` and the
native replay capture in `_run_sdk_reconciliation_operation` now pass terminal status
through `_cursor_sdk_capture_complete`. Only `finished` proves a complete native
usage capture. `cancelled`, `canceled`, `interrupted`, failure and other non-finished
statuses retain valid reported counters as partial lower bounds; absent counters stay
unknown. `TokenUsageReadService`, the usage CLI, Integration API inspection and
Dashboard HTTP projection consume those authenticated attempt/category states
without changing scheduler failure or recovery policy.

Independent production-path regressions return terminal `cancelled` and
`interrupted` with independently expected total `110`; each scheduler attempt fails
while every public read reports one partial attempt, zero complete/unknown attempts,
total `110`, and partial total-category coverage. A cancelled result without counters
reports one unknown attempt and null total/category values. The control case returns
`finished` with total `110` and proves one known attempt with complete attempt and
category coverage through the same production callers.

### F-06 — deferred residual risk accepted by plan owner

Demonstrated: the F-05 tests cover production publication fencing and both native
interruption and killed-worker crash replay; a real Phase 27 adopted sequence
replacement covers the blocked
leaf, successor, shared ancestry and repeated-query deduplication. A separate
authenticated `140`-token run is then related as an unadopted review successor and
remains excluded, as does its unrelated run identity. Recovered SDK and mixed-
provider records pass Integration API/Dashboard HTTP reads with independent totals
and byte/hash comparisons. Existing Phase 28 name/version, paging and strict Python
schema consumers pass.

Affected production readers are `TokenUsageReadService.run_report` and
`.sequence_report`, the `scheduler usage` and `scheduler sequence usage` commands,
Integration API run/sequence inspection, Dashboard HTTP/API projection, and the
existing strict browser consumers. The Python production readers and HTTP/API paths
have focused fake-backed coverage; only genuine historical authority and execution
of the provisioned browser toolchain remain unavailable.

Unavailable external evidence:

- the original authenticated pre-25 scheduler ledger and protected artifacts, with
  provenance and hashes, containing token-bearing Codex implementer and reviewer
  captures do not exist in the checkout, as documented under U-04;
- strict browser TypeScript consumers remain unexecuted because no compatible
  Node/npm runtime is provisioned.

The plan owner, Rodrigo Badia, decided on 2026-10-18 05:15 BOT (UTC-4) to defer
these exact gaps as accepted residual risk. They do not keep F-06 or U-04 incomplete,
do not fail `tests_status`, and do not authorize fallback authority, live capture or
workstation installation. The preceding corrections fixed the confirmed read-only,
capture, completeness and reconciliation defects; no remaining code defect is being
hidden by this decision.

## Deferred residual risk (plan owner 2026-10-18)

- **D-01 — status: deferred.** Genuine pre-Phase-25 historical ledger/artifact
  captures containing token-bearing `turn.completed.usage` for both authenticated
  Codex roles do not exist in the checkout. They were not fabricated, generated with
  Phase 25.4 code or captured live. Existing genuine historical fixtures prove
  runtime-independent reads and honest unknown coverage where counters are absent;
  fake-backed authenticated fixtures prove the two-role projection path.
- **D-02 — status: deferred.** `npm run typecheck` and the affected focused Vitest/
  browser validation were not executed in this reapplication because Node/npm is not
  provisioned. No toolchain was installed and no packaged Dashboard asset was
  regenerated. Python Integration API/Dashboard HTTP compatibility remains mandatory
  and is included in the focused validation matrix.

## Exact validation

Final focused validation used isolated roots:

```text
HOME=/tmp/ai-dev-loop-phase254-correction-final-matrix3/home
XDG_STATE_HOME=/tmp/ai-dev-loop-phase254-correction-final-matrix3/state
XDG_CONFIG_HOME=/tmp/ai-dev-loop-phase254-correction-final-matrix3/config
XDG_CACHE_HOME=/tmp/ai-dev-loop-phase254-correction-final-matrix3/cache
TMPDIR=/tmp/ai-dev-loop-phase254-correction-final-matrix3/tmp
```

The four-file correction subset used `/tmp/ai-dev-loop-phase254-correction-final-core`;
the adjacent SDK/recovery/HTTP run used
`/tmp/ai-dev-loop-phase254-correction-final-adjacent`. The exact reviewer probes used
`/tmp/ai-dev-loop-phase254-correction-final-review-probes`; the new F-04/F-05
selection used `/tmp/ai-dev-loop-phase254-correction-final-f04f05`; and the unit run
used `/tmp/ai-dev-loop-phase254-correction-f04f05-unit`. A final capture-safety
rerun used `/tmp/ai-dev-loop-phase254-correction-final-postsafe`.

The latest F-05/F-07 correction used fixed isolated roots
`/tmp/ai-dev-loop-phase254-review4-focused`,
`/tmp/ai-dev-loop-phase254-review4-probes`,
`/tmp/ai-dev-loop-phase254-review4-adjacent`, and
`/tmp/ai-dev-loop-phase254-review4-quality`. No live scheduler root or real provider
credential was used; tests used only their isolated fake credential/backend.

The preserved-patch reapplication and plan-owner deferral validation used isolated
roots below `/tmp/ai-dev-loop-phase254-reapply/`. Executed and passed on 2026-10-18:

```bash
HOME=/tmp/ai-dev-loop-phase254-reapply/focused/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-phase254-reapply/focused/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-phase254-reapply/focused/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-phase254-reapply/focused/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-phase254-reapply/focused/cache \
TMPDIR=/tmp/ai-dev-loop-phase254-reapply/focused/tmp \
TMP=/tmp/ai-dev-loop-phase254-reapply/focused/tmp \
TEMP=/tmp/ai-dev-loop-phase254-reapply/focused/tmp \
.venv/bin/python -m pytest -q tests/unit/scheduler/test_phase25_4_token_usage.py tests/integration/test_phase25_4_token_usage.py tests/unit/integration_api/test_phase25_4_historical_usage.py tests/unit/scheduler/test_schema_readonly_historical.py tests/integration/test_phase21_2_run_inspection.py tests/integration/test_phase21_3_sequence_inspection.py tests/integration/test_phase21_4_review_inspection.py tests/unit/integration_api/test_contract.py tests/unit/integration_api/test_output_projection.py tests/integration/test_phase25_4_dashboard_compatibility.py tests/unit/dashboard tests/integration/test_phase26_1_dashboard_queries.py tests/integration/test_phase26_2_dashboard_server.py tests/integration/test_phase27_3_inspection.py tests/integration/test_phase28_dashboard_paging.py
# 184 passed in 251.25s

.venv/bin/python -m ruff format --check .
# 412 files already formatted
.venv/bin/python -m ruff check .
# All checks passed
.venv/bin/python -m mypy src/ai_dev_loop
# Success: no issues found in 214 source files
.venv/bin/python -m mkdocs build --strict
# exit 0; documentation built in 0.87s (existing MkDocs 2.0 advisory only)
git diff --check
# exit 0, no output
```

Because the real index is sandbox-read-only, a temporary index plus temporary object
store was also populated from the complete working tree. `git diff --cached --check
HEAD` passed for all 40 files. This is validation evidence only and does not claim
that the repository's real index is staged.

Executed and passed:

```bash
.venv/bin/python -m pytest -q tests/unit/scheduler/test_phase25_4_token_usage.py tests/integration/test_phase25_4_token_usage.py tests/unit/integration_api/test_phase25_4_historical_usage.py tests/unit/scheduler/test_schema_readonly_historical.py tests/integration/test_phase21_2_run_inspection.py tests/integration/test_phase21_3_sequence_inspection.py tests/integration/test_phase21_4_review_inspection.py tests/unit/integration_api/test_contract.py tests/unit/integration_api/test_output_projection.py tests/integration/test_phase25_4_dashboard_compatibility.py tests/unit/dashboard tests/integration/test_phase26_1_dashboard_queries.py tests/integration/test_phase26_2_dashboard_server.py tests/integration/test_phase27_3_inspection.py tests/integration/test_phase28_dashboard_paging.py
# 181 passed in 232.40s

.venv/bin/python -m pytest -q tests/unit/scheduler/test_phase25_4_token_usage.py tests/integration/test_phase25_4_token_usage.py tests/unit/integration_api/test_phase25_4_historical_usage.py tests/integration/test_phase25_4_dashboard_compatibility.py
# 37 passed in 91.29s

.venv/bin/python -m pytest -q tests/integration/test_phase25_2_sdk_workflow.py tests/integration/test_phase25_3_sdk_recovery.py::test_failed_worker_without_result_stops_owned_descendants_before_native_replay tests/integration/test_phase25_3_sdk_recovery.py::test_killed_reconciliation_worker_resumes_obligation_without_prompt_redispatch tests/integration/test_phase26_2_dashboard_server.py::test_c262_2_real_provider_owned_output_and_reviewer_content_boundaries
# 16 passed in 58.80s

.venv/bin/python -m pytest -q tests/unit/scheduler/test_phase25_4_token_usage.py
# 10 passed in 0.42s

.venv/bin/python -m pytest -q tests/integration/test_phase25_4_token_usage.py -k 'f04 or f05'
# 5 passed, 19 deselected in 23.43s

.venv/bin/python -m pytest -q -p tests.conftest /tmp/test_phase254_review_probes.py
# 2 passed in 8.08s

.venv/bin/python -m pytest -q tests/unit/scheduler/test_phase25_4_token_usage.py tests/integration/test_phase25_4_token_usage.py -k 'f04 or f05'
# 7 passed, 27 deselected in 23.93s (after the final safe-publication guard)

.venv/bin/python -m ruff format --check .
# 412 files already formatted

.venv/bin/python -m ruff check .
# All checks passed

.venv/bin/python -m mypy src/ai_dev_loop
# Success: no issues found in 214 source files

.venv/bin/python -m mkdocs build --strict
# exit 0; documentation built (the existing MkDocs 2.0 advisory was informational)

git diff --check HEAD
# exit 0, no output
```

Latest F-05/F-07 executed and passed:

```bash
HOME=/tmp/ai-dev-loop-phase254-review4-focused/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-phase254-review4-focused/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-phase254-review4-focused/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-phase254-review4-focused/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-phase254-review4-focused/cache \
TMPDIR=/tmp/ai-dev-loop-phase254-review4-focused/tmp \
.venv/bin/python -m pytest -q \
  tests/unit/scheduler/test_phase25_4_token_usage.py \
  tests/integration/test_phase25_4_token_usage.py \
  tests/unit/integration_api/test_phase25_4_historical_usage.py \
  tests/integration/test_phase25_4_dashboard_compatibility.py
# 40 passed in 108.73s

HOME=/tmp/ai-dev-loop-phase254-review4-probes/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-phase254-review4-probes/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-phase254-review4-probes/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-phase254-review4-probes/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-phase254-review4-probes/cache \
TMPDIR=/tmp/ai-dev-loop-phase254-review4-probes/tmp \
.venv/bin/python -m pytest -q -p tests.conftest \
  /tmp/test_phase254_review3_replay.py \
  /tmp/test_phase254_review_probes.py
# 4 passed in 15.82s

HOME=/tmp/ai-dev-loop-phase254-review4-adjacent/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-phase254-review4-adjacent/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-phase254-review4-adjacent/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-phase254-review4-adjacent/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-phase254-review4-adjacent/cache \
TMPDIR=/tmp/ai-dev-loop-phase254-review4-adjacent/tmp \
.venv/bin/python -m pytest -q \
  tests/integration/test_phase25_2_sdk_workflow.py \
  tests/integration/test_phase25_3_sdk_recovery.py::test_failed_worker_without_result_stops_owned_descendants_before_native_replay \
  tests/integration/test_phase25_3_sdk_recovery.py::test_killed_reconciliation_worker_resumes_obligation_without_prompt_redispatch
# 15 passed in 50.90s

HOME=/tmp/ai-dev-loop-phase254-review4-quality/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-phase254-review4-quality/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-phase254-review4-quality/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-phase254-review4-quality/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-phase254-review4-quality/cache \
TMPDIR=/tmp/ai-dev-loop-phase254-review4-quality/tmp \
.venv/bin/python -m ruff format --check .
# 412 files already formatted

# The same isolated environment was used for each command below.
.venv/bin/python -m ruff check .
# All checks passed
.venv/bin/python -m mypy src/ai_dev_loop
# Success: no issues found in 214 source files
.venv/bin/python -m mkdocs build --strict
# exit 0; documentation built (existing MkDocs 2.0 advisory only)
git diff --check HEAD
# exit 0, no output
```

After strengthening the final public-scope assertions, the exact post-edit rerun used
`/tmp/ai-dev-loop-phase254-review4-postdoc`:

```bash
HOME=/tmp/ai-dev-loop-phase254-review4-postdoc/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-phase254-review4-postdoc/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-phase254-review4-postdoc/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-phase254-review4-postdoc/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-phase254-review4-postdoc/cache \
TMPDIR=/tmp/ai-dev-loop-phase254-review4-postdoc/tmp \
.venv/bin/python -m pytest -q \
  tests/integration/test_phase25_4_token_usage.py -k 'sparse_replay'
# 2 passed, 25 deselected in 13.33s
# The remaining commands used the same isolated environment.
.venv/bin/python -m ruff format --check .
# 412 files already formatted
.venv/bin/python -m ruff check .
# All checks passed
git diff --check HEAD
# exit 0, no output
```

The reopened F-05/F-08 correction used fresh isolated HOME/XDG/TMP roots and only
the fake SDK/provider backends. The initial diagnostic selection, before the
production fix and before strengthening the deadline boundary assertion, executed:

```bash
HOME=/tmp/ai-dev-loop-phase254-correction/pre-fix/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-phase254-correction/pre-fix/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-phase254-correction/pre-fix/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-phase254-correction/pre-fix/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-phase254-correction/pre-fix/cache \
TMPDIR=/tmp/ai-dev-loop-phase254-correction/pre-fix/tmp \
.venv/bin/python -m pytest -q tests/integration/test_phase25_4_token_usage.py \
  -k 'deadline_usage or f08_nonfinished'
# 2 failed, 2 passed, 27 deselected in 28.33s
# Both reported-usage non-finished cases were incorrectly complete. The original
# deadline assertion passed only after reconciliation, exposing why the earlier
# F-05 test did not isolate the authenticated deadline result.
```

Before adding the explicit successful-status control, the first fixed regression
run used `/tmp/ai-dev-loop-254-postfix.r9XfCJ` and passed `4 passed, 27 deselected
in 15.40s`; the corresponding four-file matrix used
`/tmp/ai-dev-loop-254-focused.LmfzlI` and passed `44 passed in 122.66s`. The control
was then added and both selections were superseded by the final runs below rather
than treated as sufficient evidence.

After fixing production and requiring the source-attempt report at the
`cursor_sdk_reconciliation_requested` boundary, the final regression and contract
matrices executed and passed:

```bash
HOME=/tmp/ai-dev-loop-254-regressions.ZHDfnl/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-254-regressions.ZHDfnl/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-254-regressions.ZHDfnl/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-254-regressions.ZHDfnl/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-254-regressions.ZHDfnl/cache \
TMPDIR=/tmp/ai-dev-loop-254-regressions.ZHDfnl/tmp \
.venv/bin/python -m pytest -q tests/integration/test_phase25_4_token_usage.py \
  -k 'deadline_usage or f08_nonfinished'
# 5 passed, 27 deselected in 19.84s

HOME=/tmp/ai-dev-loop-254-final-matrix.uYLAi1/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-254-final-matrix.uYLAi1/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-254-final-matrix.uYLAi1/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-254-final-matrix.uYLAi1/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-254-final-matrix.uYLAi1/cache \
TMPDIR=/tmp/ai-dev-loop-254-final-matrix.uYLAi1/tmp \
.venv/bin/python -m pytest -q \
  tests/unit/scheduler/test_phase25_4_token_usage.py \
  tests/unit/integration_api/test_phase25_4_historical_usage.py \
  tests/integration/test_phase25_4_token_usage.py \
  tests/integration/test_phase25_4_dashboard_compatibility.py
# 45 passed in 127.40s

HOME=/tmp/ai-dev-loop-254-adjacent.W4n2Pp/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-254-adjacent.W4n2Pp/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-254-adjacent.W4n2Pp/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-254-adjacent.W4n2Pp/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-254-adjacent.W4n2Pp/cache \
TMPDIR=/tmp/ai-dev-loop-254-adjacent.W4n2Pp/tmp \
.venv/bin/python -m pytest -q \
  tests/integration/test_phase25_2_sdk_workflow.py \
  tests/integration/test_phase25_3_sdk_recovery.py::test_failed_worker_without_result_stops_owned_descendants_before_native_replay
# 14 passed in 44.55s
```

The first final Ruff formatting check correctly reported the newly added parameter
table as the sole unformatted Python file (`1 file would be reformatted, 411 files
already formatted`, exit 1). Ruff lint, mypy, strict MkDocs and
`git diff --check HEAD` all passed in that same isolated quality run. Ruff then
mechanically formatted the test, and the affected regression was rerun:

```bash
HOME=/tmp/ai-dev-loop-254-postformat.EKonnD/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-254-postformat.EKonnD/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-254-postformat.EKonnD/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-254-postformat.EKonnD/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-254-postformat.EKonnD/cache \
TMPDIR=/tmp/ai-dev-loop-254-postformat.EKonnD/tmp \
.venv/bin/python -m pytest -q tests/integration/test_phase25_4_token_usage.py \
  -k 'deadline_usage or f08_nonfinished'
# 5 passed, 27 deselected in 20.50s
```

Final quality validation used `/tmp/ai-dev-loop-254-quality-final` for isolated
HOME/XDG/TMP, mypy cache and MkDocs output:

```bash
.venv/bin/python -m ruff format --check .
# 412 files already formatted
.venv/bin/python -m ruff check .
# All checks passed
MYPY_CACHE_DIR=/tmp/ai-dev-loop-254-quality-final/mypy-cache \
  .venv/bin/python -m mypy src/ai_dev_loop
# Success: no issues found in 214 source files
.venv/bin/python -m mkdocs build --strict \
  --site-dir /tmp/ai-dev-loop-254-quality-final/site
# exit 0; documentation built in 0.75s (existing MkDocs 2.0 advisory only)
git diff --check HEAD
# exit 0, no output
```

The subsequent repeated-deadline F-05 correction also used only fake providers and
fresh isolated state. Before the shared-carrier fix, the new production test proved
that the repeat fired during close and then failed the pre-reconciliation total:

```bash
HOME=/tmp/ai-dev-loop-254-repeat-prefix.xczZGR/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-254-repeat-prefix.xczZGR/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-254-repeat-prefix.xczZGR/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-254-repeat-prefix.xczZGR/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-254-repeat-prefix.xczZGR/cache \
TMPDIR=/tmp/ai-dev-loop-254-repeat-prefix.xczZGR/tmp \
.venv/bin/python -m pytest -q \
  tests/integration/test_phase25_4_token_usage.py::test_f05_repeated_deadline_during_close_preserves_authenticated_usage
# 1 failed in 5.22s: expected 110, received null/unknown before reconciliation
```

After the production fix, the unchanged new test passed alone; the combined
single/repeated deadline and F-08 selection, full focused matrix and adjacent SDK/
recovery matrix then passed:

```bash
HOME=/tmp/ai-dev-loop-254-repeat-postfix.a07AvZ/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-254-repeat-postfix.a07AvZ/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-254-repeat-postfix.a07AvZ/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-254-repeat-postfix.a07AvZ/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-254-repeat-postfix.a07AvZ/cache \
TMPDIR=/tmp/ai-dev-loop-254-repeat-postfix.a07AvZ/tmp \
.venv/bin/python -m pytest -q \
  tests/integration/test_phase25_4_token_usage.py::test_f05_repeated_deadline_during_close_preserves_authenticated_usage
# 1 passed in 8.05s

HOME=/tmp/ai-dev-loop-254-repeat-affected.tfjxbh/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-254-repeat-affected.tfjxbh/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-254-repeat-affected.tfjxbh/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-254-repeat-affected.tfjxbh/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-254-repeat-affected.tfjxbh/cache \
TMPDIR=/tmp/ai-dev-loop-254-repeat-affected.tfjxbh/tmp \
.venv/bin/python -m pytest -q tests/integration/test_phase25_4_token_usage.py \
  -k 'deadline_usage or repeated_deadline or f08_nonfinished'
# 6 passed, 27 deselected in 24.76s

HOME=/tmp/ai-dev-loop-254-repeat-matrix/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-254-repeat-matrix/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-254-repeat-matrix/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-254-repeat-matrix/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-254-repeat-matrix/cache \
TMPDIR=/tmp/ai-dev-loop-254-repeat-matrix/tmp \
.venv/bin/python -m pytest -q \
  tests/unit/scheduler/test_phase25_4_token_usage.py \
  tests/unit/integration_api/test_phase25_4_historical_usage.py \
  tests/integration/test_phase25_4_token_usage.py \
  tests/integration/test_phase25_4_dashboard_compatibility.py
# 46 passed in 143.11s

HOME=/tmp/ai-dev-loop-254-repeat-adjacent/home \
XDG_CONFIG_HOME=/tmp/ai-dev-loop-254-repeat-adjacent/config \
XDG_DATA_HOME=/tmp/ai-dev-loop-254-repeat-adjacent/data \
XDG_STATE_HOME=/tmp/ai-dev-loop-254-repeat-adjacent/state \
XDG_CACHE_HOME=/tmp/ai-dev-loop-254-repeat-adjacent/cache \
TMPDIR=/tmp/ai-dev-loop-254-repeat-adjacent/tmp \
.venv/bin/python -m pytest -q \
  tests/integration/test_phase25_2_sdk_workflow.py \
  tests/integration/test_phase25_3_sdk_recovery.py::test_failed_worker_without_result_stops_owned_descendants_before_native_replay
# 14 passed in 53.65s

.venv/bin/python -m ruff format --check \
  src/ai_dev_loop/scheduler/cursor_attempt_runner.py \
  tests/integration/test_phase25_2_sdk_workflow.py \
  tests/integration/test_phase25_4_token_usage.py
# 3 files already formatted
.venv/bin/python -m ruff check \
  src/ai_dev_loop/scheduler/cursor_attempt_runner.py \
  tests/integration/test_phase25_2_sdk_workflow.py \
  tests/integration/test_phase25_4_token_usage.py
# All checks passed
```

Final repeated-deadline quality validation used
`/tmp/ai-dev-loop-254-repeat-quality` for isolated HOME/XDG/TMP, mypy cache and
MkDocs output:

```bash
.venv/bin/python -m ruff format --check .
# 412 files already formatted
.venv/bin/python -m ruff check .
# All checks passed
MYPY_CACHE_DIR=/tmp/ai-dev-loop-254-repeat-quality/mypy-cache \
  .venv/bin/python -m mypy src/ai_dev_loop
# Success: no issues found in 214 source files
.venv/bin/python -m mkdocs build --strict \
  --site-dir /tmp/ai-dev-loop-254-repeat-quality/site
# exit 0; documentation built in 0.90s (existing MkDocs 2.0 advisory only)
git diff --check HEAD
# exit 0, no output
```

After strengthening the repeated-deadline regression to assert CLI, Integration API
and Dashboard recovery-lineage projections plus exactly one reconciliation attempt,
`/tmp/ai-dev-loop-254-repeat-finalprobe.1tiXJU` passed the test (`1 passed in
7.04s`). The affected test then passed Ruff format (`1 file already formatted`) and
lint, and `git diff --check HEAD` remained clean.

Historical aborted-run frontend attempts failed because the required executable/
runtime was absent:

```bash
cd dashboard-web && npm run typecheck
# exit 127: npm: command not found

cd dashboard-web && npm test -- --run src/dashboard/local-fetch-data-source.test.ts src/dashboard/AttemptTimeline.test.tsx
# exit 127: npm: command not found

command -v npm
# exit 1 with no output on 2026-10-18; the blocked frontend commands were not
# repeatedly invoked without a provisioned runtime
```

Read-only follow-up found project-local `tsc`/`vitest` JavaScript packages but no
`node`, `nodejs`, `npm`, `bun`, `deno` or `corepack`; invoking their scripts cannot
work without a runtime. Nothing was installed.

The latest correction reconfirmed that all six executables remain unavailable by
running only `command -v`; it did not repeat the two impossible frontend commands.

One ad hoc post-edit command explicitly passed this Markdown findings file to Ruff
and exited 2 because Ruff Markdown formatting requires preview mode. The supported
repository command `.venv/bin/python -m ruff format --check .` was rerun immediately
and passed with all 412 Python files formatted; no preview mode or bypass was added.

Development-only focused runs initially exposed and then corrected: API 1.11 test
expectations after the 1.12 bump; a fake implementer usage counter that did not
advance across corrections; and historical inspect ingestion missing Phase 28
additive name defaults. The recovery-lineage fixture also first supplied a non-UUID
resubmission ID and correctly failed admission; it passed after using the required
UUID form. In the 2026-10-18 correction, the external probes first reproduced both
reviewer failures (`2 failed` after loading the repository fixtures) and then passed
unchanged (`2 passed`). The first new F-04/F-05 selection had one fixture-definition
failure because a sequence manifest contained only one phase; the production test
was corrected to use the supported two-phase manifest and the selection passed. A
first mypy run found three local type-inference errors from a reused candidate name;
renaming it made the full 214-file mypy check pass. The final 181-test matrix above
reran the affected paths and passed.

For the latest correction, both new review3 reproductions first failed independently:
the sparse event-to-cumulative replay reported `220`, and absent stream reasoning
conflicted with terminal reasoning. After the production correction, the unchanged
review3 file passed both cases; it also passed together with the earlier F-04/F-05
reviewer probes (`4 passed`). A manifest-v4 sequence probe initially used an
unsupported manifest-v3 helper and correctly failed preparation before publication;
the tracked regression uses the required v4 surface and passed. No production
fallback or contract waiver was added.

Intentionally unexecuted:

- D-01 genuine pre-25 token-bearing historical captures for both authenticated Codex
  roles: the required source bundle does not exist; accepted residual risk, status
  `deferred` by the plan owner on 2026-10-18.
- D-02 `npm run typecheck` and focused Vitest/browser commands: no Node/npm-compatible
  runtime is provisioned; accepted residual risk, status `deferred` by the plan owner
  on 2026-10-18. Read-only `command -v` checks reconfirmed `node`, `nodejs`, `npm`,
  `bun`, `deno` and `corepack` are absent. No toolchain was installed.
- Repository-wide pytest: reserved for Phase 25.5 by the approved plan.
- Real Cursor/Codex/model/provider calls, live scheduler/timer/integration work,
  installation, cleanup, destructive/external Git actions: prohibited.
- Dashboard asset regeneration: no frontend source or packaged asset changed.

## Unfinished mandatory work and safe next actions

No mandatory Phase 25.4 product or focused-test work remains. D-01 and D-02 are
explicitly deferred residual risks, not fabricated evidence and not Phase 25.4
blockers. If the genuine provenance bundle or an already provisioned Node/npm
environment later becomes available, the corresponding checks may be run without
changing this acceptance decision; no toolchain installation is authorized here.

The repository-wide pytest gate remains Phase 25.5 work. The carryover snapshot is
already staged and this correction leaves only its scoped overlays for the owning
orchestrator's promised final `git add -A` normalization before reviewer B begins.
No commit, checkpoint, push, PR or merge was performed; the scheduler retains sole
authority to normalize final staging and create accepted intermediate sequence
checkpoints.

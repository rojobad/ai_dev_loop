# Phase 25.2 — SDK identity, worker and authenticated evidence

## Goals

Connect new SDK inputs to versioned submit/prepare bindings and the existing
owned attempt worker. Complete fake-backed normal initial/review/correction and
sequence flow while preserving historical reads and exact reviewer identity.

## Non-Goals

Enabling SDK retries/recovery before 25.3, changing retry ceilings/timeouts,
metrics aggregation, migrating old conversations or deploying intermediate code.

## Scope

SDK identity types, frozen config/sequence contracts, durable create/send intent,
worker and preflight integration, bounded raw event/result evidence, lifecycle
ownership, normal Cursor/Codex loop, historical readers and directly affected
integration output reads, docs, schemas and tests.

## Out of Scope

Phase 24, root YAML, model calls/credentials/installation/timer changes,
old-session execution migration, recovery authority expansions, Codex model or
identity changes, metrics presentation, provider switching and skill edits.

## Required Context

Read [the overview](phase-25-cursor-sdk-and-token-usage.md), accepted
[25.1](phase-25-1-sdk-foundation-and-configuration.md) findings and
[POC](../findings/phase-25-sdk-poc/README.md). Verify F-01–F-04 in the checkout.
Inspect scheduler domain `common.py`, `state.py`, `events.py`, `sequence.py`,
recovery records; application `submission.py`, `sequence_prepare.py`,
`sequence_materializer.py`, `scheduler_preflight.py`, `attempt_service.py`,
`cursor_workflow_service.py`, `cursor_evidence.py`, `run_state_bridge.py`,
`systemd_backend.py`, `attempt_envelope.py`; `cursor_attempt_runner.py`,
`process.py`, infrastructure paths/SQLite/protected artifacts and Integration API
process-output readers. Keep the Codex worker/builders as reviewer authority.

## Cursor Rules And Skills

Follow `AGENTS.md` and the eight `.cursor/rules/` listed by the overview. Read
`.agents/skills/create-cursor-plan/SKILL.md`; preserve it and the separate
read-only `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`.
No `.cursor/skills/` exists. Update affected rule statements about positional CLI
prompts/chat UUIDs only when their SDK replacements are implemented and tested.

## Architecture Guardrails

- Domain types contain plain typed identifiers/options, never SDK handles.
  Cursor-specific IDs accept exact observed agent-prefixed/bare UUID and native
  run-prefixed UUID forms. Codex UUID validation remains unchanged.
- Introduce distinct SDK context/state/event/sequence/invocation schema versions
  using the next free version for each changed contract. Record those versions
  and canonical fields in findings. Frozen SDK objects are discriminated by
  runtime/version and required non-null fields; old missing fields retain their
  historical CLI meaning. Do not broadly relax old UUID or extra-key validators.
- New runtime binding includes conversation_owner_run_id, conversation_key,
  exact agent_id and a typed optional native_run_id only before send confirmation;
  native_run_id is mandatory in confirmed running/terminal evidence. SDK ID null
  never authorizes creating a replacement for an uncertain dispatched operation.
- Freeze model+parameters atomically in submit/prepare and per-phase overrides,
  SDK version, settings, tool/sandbox policy and credential reference. Resolve
  native root from scheduler state home and stable key, recording its authenticated
  logical binding, without exposing absolute private paths to target repositories.
- Submit/prepare do not launch a bridge, read a credential or query the catalog.
  Bounded preflight checks installed package version, health/auth/catalog, explicit
  permissions/config sources and store ownership before inference. Reject absent
  extra/key, unsupported selection or required configuration safely.
- SDK adapter lives inside the isolated owned worker. Keep exact attempt/unit
  binding, bounded capture, fences and repository reservation. Protect bridge
  runtime and tools using the existing process group/unit ownership, not just the
  launcher's PID. No live systemd/timer installation in automated tests.
- Old snapshot/event/artifact readers must continue working in this slice;
  execution is still intermediate, so maintain the original CLI paths only until
  the final 25.5 retirement. No SDK fallback for old CLI-bound inputs.

## Implementation Plan

**W-01 — Frozen inputs and versioned identity.** Replace 25.1's submit/prepare
guard with actual SDK bindings. Propagate the complete selection through lazy
sequence materialization and context-copy callers. Add new manifest shapes for
SDK overrides while retaining old schema readers. Update SQLite filters that
hard-code context schema 4 for sequences to recognize semantic sequence binding
across versions. Prove hashes/idempotency differ when authority-bearing inputs
change and later YAML changes do not alter a frozen definition.

**W-02 — Durable creation and send correlation.** Persist authenticated creation
intent under the owned attempt before adapter create. Bind its returned exact
agent ID/store ownership before any inference. Persist send intent with exact
prompt digest, agent/store identity and attempt/dispatch fence before send; record
native run ID immediately upon return using a durable incremental journal binding
readable by reconciliation. Normal completion publishes authenticated outcome and
ledger transition once. Never concatenate partial thoughts/tool text into the
final result or manufacture CLI stream-json from SDK messages.

For create/send accepted without durable identity, mark uncertainty and retain
ownership; no inferred success, duplicate send or guessed ID. 25.3 owns convergent
reconciliation. Expose safe diagnostics and abort/inspection at this boundary.

**W-03 — Owned worker and bounded evidence.** Wire the port into the real attempt
worker/backend. Capture typed SDK envelopes as NDJSON with native offsets where
present, separate stderr, final result and safe metadata with explicit format and
version. Preserve raw envelopes/partial output privately; redact secrets before
publication. Retain current byte bounds/atomic hashes and protect against capture
failure. Keep exactly one terminal completion signal and authenticated returned
identities. Extend explicit Integration API output reads to the SDK format;
summary surfaces must not leak raw content/keys/full identities.

Cover normal bridge closure and timeout/abort stopping the **entire owned tree**.
Unproved termination holds the reservation and reports reconciliation pending.
No retry is dispatched in this slice for SDK failures; put an explicit temporary
runtime-specific guard on automatic timeout/capacity continuation and cursor
recovery eligibility. Historical CLI retry semantics remain intermediate-only.

**W-04 — Normal production workflow.** Drive submit/start/tick -> SDK initial
implementation -> existing staging -> Codex bootstrap -> exact fix -> same SDK
agent correction -> exact Codex resume -> accepted result. Wire normal sequence
non-final checkpoint and next-phase handoff, preserving independent agents per
new conversation and existing final staging behavior. Cancellation suppresses
late completion and later agent/staging/review work. Verify all changed reader
and content-auth callers, not only the SDK runner.

Boundary: queued frozen SDK context -> existing admission/reservation -> persisted
create intent -> exact durable agent binding -> persisted send intent -> native
run binding -> authenticated terminal evidence -> fenced workflow transition.
On interruption, preserve intent/evidence/reservation and the intermediate safe
guard; idempotent ingestion of known completed results must not launch another
turn. Cancel requests are durable before process stop. SDK status alone never
authorizes acceptance or a checkpoint commit.

## Testing Criteria

Add `tests/unit/scheduler/test_phase25_2_sdk_contracts.py`,
`tests/unit/scheduler/test_phase25_2_sdk_worker.py` and
`tests/integration/test_phase25_2_sdk_workflow.py`. Capture genuine pre-25 fixture
contexts, states, manifests, events/invocations and ledger rows with baseline
provenance under `tests/fixtures/phase25_historical/`; reuse valid existing Phase
21/22/23 fixtures. Capture from old writers before replacing them, without live
agents or editing old bytes to add SDK fields. Use fake bridge child trees, fake
SDK events and fake Codex; temporary homes/keys are synthetic.

| Contract | Acceptance scenario through production callers |
| --- | --- |
| W-01 | CLI submit/sequence prepare, store reload/materialization and sequence listing preserve SDK fields/override precedence; old hashes/readers remain valid; malformed IDs cannot relax B identity. |
| W-02 | Barriers prove agent binding precedes send, send intent precedes inference and returned run ID is journaled. Lost create/send result produces uncertainty, zero automatic redispatch and no released reservation. |
| W-03 | Actual worker/backend stops bridge and descendant tool on timeout/abort; launcher-only kill is insufficient. Partial/bounded capture and secret sentinels are retained/redacted correctly; explicit content reads authenticate SDK artifacts. |
| W-04 | Real scheduler loop accepts normal implementation/correction and two-phase sequence with fake SDK/Codex; exact prompts/B are preserved, only non-final checkpoint commits occur, abort prevents late adoption. |

The temporary retry guard must be tested through automatic tick and public
cursor-retry check/mutation callers. Do not defer termination safety, historical
reader compatibility or normal workflow wiring to later acceptance.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase25_2_sdk_contracts.py tests/unit/scheduler/test_phase25_2_sdk_worker.py tests/integration/test_phase25_2_sdk_workflow.py tests/unit/scheduler/test_schema.py tests/unit/scheduler/test_schema_readonly_historical.py tests/integration/test_phase17_4_cursor_workflow.py tests/integration/test_phase17_5_scheduler_review_loop.py tests/unit/scheduler/test_attempt_executor.py tests/integration/test_phase20_1_sequence_prepare.py tests/integration/test_phase20_3_sequence_handoff.py
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Record W-01–W-04, changed schema numbers, canonical bindings, fixture provenance
and exact results in `archive/implementation-history/findings/phase-25-2-sdk-worker-findings.md`.
Select affected Integration API output tests when those readers change. Full-suite
gate is 25.5, not a substitute for these mandatory production-path tests.

## Risks Or Recovery Notes

Do not install this intermediate package into a live worker environment. 25.3
must replace explicit SDK recovery guards before production cutover. Preserve
uncertain intents and orphan diagnostics; do not edit the native store or create
another agent to conceal a lost response.

## OpenQuestions

None. Accepted 25.1 is a required prerequisite.

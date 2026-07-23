# Phase 16.7 -> Phase 16.8 Handoff

Date: 2026-07-23 (A/B review completed; content-bound `commit_patch` identity)

## Executive status

Phase 16.7 connects the Phase 16.3–16.6 stack into the first complete, locally
testable `pr-review-v2` orchestration path:

- temporary public CLI namespace `ai_dev_loop pr-review-v2`;
- optional isolated config section `pr_review_v2` (schema version / `version` remains `1`);
- `create` / `prepare` end only in durable `PreparedState` (no workers/agents/writes);
- explicit `start RUN_ID` is the sole external-effects gate;
- all three LOCAL effects execute through the Phase 16.4 outbox/claim/fencing path:
  `GeneratePublicationTextEffect`, `AdjudicateThreadsEffect`, `RunLocalFixEffect`;
- exact frozen Codex reviewer session resume (never `--last`);
- deterministic per-cycle subordinate Phase 16.2 carrier via `run_local_review_fix()`;
- control plane: `status`, `history`, `resume` (with `--confirm-user-continuation`),
  `abort` (persist-first + capture carrier identity before `AbortedState`, then
  carrier Cursor → owned Codex children → supervisor signaling).

Legacy `pr-review` / `github` behavior is unchanged. No SQLite migration was added.

## A/B review closure and active durations

The detached A/B implementation run completed after **8 of 10** available review
iterations. Iterations 1--7 produced corrective work; iteration 8 reported no
actionable findings. The times below are the recorded active subprocess durations
only (Cursor implementation/correction plus Codex staged review). They exclude
queueing and the deliberate human pause between iterations 7 and 8.

| Iteration | Work kind | Cursor | Codex review | Active total | Findings | Result |
|---:|---|---:|---:|---:|---:|---|
| 1 | Initial implementation | 49m 52s | 7m 21s | 57m 13s | 9 | corrective prompt issued |
| 2 | Correction | 23m 29s | 7m 05s | 30m 35s | 6 | corrective prompt issued |
| 3 | Correction | 22m 28s | 5m 05s | 27m 33s | 7 | corrective prompt issued |
| 4 | Correction | 21m 57s | 5m 18s | 27m 15s | 4 | corrective prompt issued |
| 5 | Correction | 13m 42s | 4m 08s | 17m 50s | 2 | corrective prompt issued |
| 6 | Correction | 14m 01s | 4m 23s | 18m 24s | 1 | corrective prompt issued |
| 7 | Correction | 11m 51s | 3m 24s | 15m 15s | 1 | budget extended for one final correction |
| 8 | Correction | 10m 14s | 3m 05s | 13m 19s | 0 | completed; no actionable findings |
| **Total** |  | **2h 47m 35s** | **39m 50s** | **3h 27m 24s** |  |  |

The final review specifically confirmed that the initial and fix `commit_patch`
operations now have distinct content-bound effect IDs, idempotency keys, and
trailers, while retries of one logical commit preserve its identity. The staged
patch is the acceptance target; this handoff does not authorize a commit, push,
or any live GitHub action.

## CLI / config / schema contracts

### CLI (`ai_dev_loop pr-review-v2`)

| Command | Semantics |
|---|---|
| `create SOURCE_RUN_ID` | Freeze SourceRunOrigin PreparedState; no external effects |
| `prepare --repo --pr --codex-session-id --plan --prompt` | Existing-PR prepare via read-only `gh api` discovery + PreparationService |
| `start RUN_ID` | Apply `StartRequested` then spawn/repair supervisor |
| `status RUN_ID` | Privacy-safe durable status + launcher liveness |
| `history RUN_ID` | Bounded redacted journal (`--limit`, `--newest`) |
| `resume RUN_ID` | Resume paused / repair supervisor; waiting_for_user needs `--confirm-user-continuation` |
| `abort RUN_ID` | Persist abort, then signal owned Cursor/Codex children and supervisor |

### Config (`pr_review_v2`)

Optional section; disabled by default; mirrors the plan YAML shape; validates
heartbeat < lease TTL, argv-safe commands, secret-key rejection, no-findings
prefixes when enabled.

### Protected artifacts (new)

- `ai_dev_loop.pr_review_v2.execution_context` v1
- `ai_dev_loop.pr_review_v2.publication_generation` v1
- `ai_dev_loop.pr_review_v2.external_adjudication` v1
- `ai_dev_loop.pr_review_v2.local_fix_result` v1
- `ai_dev_loop.pr_review_v2.operator_continuation` v1
- Package JSON schemas for Codex output:
  `pr-review-v2-publication-generation-v1.json`,
  `pr-review-v2-external-adjudication-v1.json`
- Protected source copies: `local/source/plan.md`, `local/source/prompt.txt`

## File inventory (Phase 16.7)

### Added

- `application/control_contracts.py`, `control.py`, `execution_context.py`, `preparation.py`
- `infrastructure/protected_result_store.py`, `codex_local_runners.py`, `local_fix_adapter.py`,
  `existing_pr_discovery.py`
- `workers/local_executor.py`, `workers/supervisor.py`, `workers/spawn.py`, `workers/owned_children.py`
- `pr_review_v2_supervisor_worker.py`, `pr_review_v2_carrier.py`, `runtime_factory.py`
- `commands/pr_review_v2.py`
- schemas: `pr-review-v2-*-v1.json`; `project-config-v1.json` gains `pr_review_v2`
- tests: `test_pr_review_v2_config.py`, `test_protected_result_store.py`,
  `test_local_fix_paused_resumable.py`, `test_codex_local_runners.py`,
  `test_phase16_7_create_or_reuse.py`, `test_phase16_7_local_executor.py`,
  `test_phase16_7_simulated_e2e.py`, `test_existing_pr_discovery.py`,
  `test_phase16_7_correction_regressions.py`, `test_phase16_7_correction_round2.py`,
  `test_phase16_7_correction_round3.py`, `test_phase16_7_correction_round4.py`,
  `test_phase16_7_correction_round5.py`, `test_phase16_7_correction_round6.py`,
  `test_write_contracts.py`

### Modified

- `config.py` (`PrReviewV2Section`), `cli.py` (subtree registration)
- `application/engine.py` (`create_or_reuse_prepared_run`, `fire_due_timers_for_run`)
- `infrastructure/sqlite_store.py` (nonterminal listing, run-filtered timers, history)
- `infrastructure/runtime.py` (hash overlong completion/dispatch/timer ids — no schema change)
- `domain/effects.py` (content-bound `commit_patch` identity) and `domain/reducer.py` (PAUSED local fix keeps `resumable=RunningLocalFixState`; initial/fix commit identity wiring)
- `workers/effect_executor_router.py` (optional `LocalEffectExecutor`)
- docs: `cli.md`, `configuracion.md`, `seguridad-privacidad.md`
- architecture test carve-out for `local_review_loop` in adapter/executor files

## Subordinate local-carrier lifecycle

1. Identity: `prv2c-{sha256(v2_run_id:cycle:effect_id)[:32]}`
2. Concrete `FilesystemLocalCarrierRuntime` (`ai_dev_loop.pr_review_v2_carrier`)
   seeds/reopens XDG `RunState` under project `prv2-carrier` from protected
   plan/prompt byte copies (never mutable repo content, never placeholder prompts)
   and the complete frozen execution context (Cursor output_format/force/
   trust_workspace/sandbox, Codex sandbox, cursor/codex timeouts)
3. Progress is detected before seeding: START only a new carrier; RESUME an
   interrupted non-terminal carrier with progress. Before START, RESUME, or
   terminal replay, `verify_carrier_bindings` fail-closes on drift vs
   `CarrierSeed` (repo root/HEAD, expected head branch vs stored + live branch/
   HEAD, canonical plan/prompt `snapshot_path` values resolved safely inside
   the carrier root, frozen plan/prompt/fix-prompt bytes+hashes, Cursor chat
   when seed non-null, Cursor command/model/output/force/trust/sandbox, Codex
   session/command/model/reasoning/skill/sandbox, workflow limits/timeouts).
   Null prepared chat → newly created exact chat remains the legitimate
   ExistingPr first-cycle transition.
4. Existing-PR prepared chat may be null; the exact chat from the first accepted
   hash-verified local-fix protected result is carried into every later carrier
5. Sole correction boundary: `run_local_review_fix()` with `ScheduledCursorTurn`
   referencing the protected adjudicated fix prompt (`prompts/fixes/01.txt`)
6. Accepted finalizer marks carrier `completed` / `completed_with_residual_risk`
   under Phase 16.2 locks, durably records `staged_diff_sha256` alongside the
   staged patch path, then returns `needs_external_continuation=True`. Normal
   acceptance mapping (`_map_result`) and terminal crash-replay both consume
   that recorded digest through the same verified reader: carrier-root-confined
   safe path resolution, regular owner-protected file, matching SHA-256, and
   shared `DEFAULT_MAX_PATCH_BYTES` (Phase 16.6 write gateway).
   `ProtectedResultStore.persist_patch_bytes` enforces the same size bound as
   defense in depth before writing the v2 protected patch.
7. **Terminal-carrier recoverable replay:** if the carrier is already
   `completed` / `completed_with_residual_risk` but the effect-bound v2
   `local_fix_result` is absent, reconstruct only when binding verification
   passes and evidence is complete: carrier-root-confined safe-relative
   resolution of `staged_diff_path` and Codex `result_path` (reject absolute,
   `..`, unsafe segments, final symlinks, symlinked parents), regular bounded
   owner-protected patch bytes matching recorded `staged_diff_sha256`, plus
   non-symlink bounded Codex result file with matching `result_sha256` and
   canonical `CodexReviewResult` proving accepted/no-findings or residual-risk
   terminal outcome — then persist/replay the v2 result without `RESUME` or
   agents. Missing sections/paths/hashes/files, symlinks, oversized artifacts,
   malformed JSON, actionable findings, or hash drift block reconstruction and
   must not persist an accepted v2 local-fix result. Post-finalization patch
   byte mutation before normal mapping likewise fails closed.
8. Accepted `new_head_sha` = current pre-commit HEAD (commit SHA advances only via
   later `CommitRecordedOutcome`)
9. Artifacts copied into v2 run root with hash verification

## Worker / supervisor lifecycle

- Dedicated `PrReviewV2Supervisor` processes one claimed effect at a time
- Production entry: `python -m ai_dev_loop.pr_review_v2_supervisor_worker <run-id> <token>`
- Runtime factory loads hash-verified execution context and assembles Phase 16.5
  read, Phase 16.6 write/reconcile, and Phase 16.7 LOCAL executors
- Fires only due timers for the selected run
- Future `waiting_retry` keeps the supervisor alive with bounded interruptible waits
  (no SQLite transaction or lease held while sleeping)
- Idle waits do not hold SQLite transactions or leases
- Launcher metadata: token, PID, PGID, process start time, executable, run binding
- Owned LOCAL children (Cursor/Codex) registered with the same ownership fields
- Liveness/abort validate ownership against live OS identity (refuse stale/reused PIDs)
- `abort` captures validated `RunLocalFixEffect` + deterministic carrier identity
  **before** persisting `AbortedState`, then writes the carrier abort request and
  terminates/reaps the carrier Cursor child, then owned Codex children, then the
  supervisor (abort-first + claim fencing preserved)
- Supervisor installs SIGTERM/SIGINT cancellation handling
- `ProcessCodexRunner` uses an explicit minimal environment, bounded stdout/stderr
  capture, multiplexed nonblocking stdin delivery under the same deadline as
  stdout/stderr reads (no unbounded synchronous stdin write before timeout),
  TERM→KILL with mandatory reap before clearing ownership metadata, and
  bounded result-file reads with scratch cleanup in `finally`
- `start`/`resume` return `spawn_failed` when launch/metadata persistence fails;
  never claim `spawned` without an owned process
- Late results after abort remain fenced by `complete_claim`

## LOCAL effect hardening

- Publication / adjudication / local-fix caches are effect-bound
  (run/effect/cycle/head + snapshot/context/session or fix-prompt identities)
- Fix prompts and reply texts are content-addressed
  (`local/fix-prompts/{sha256}.txt`, `local/replies/{sha256}.txt`) so cached
  replay reproduces exact refs across cycles without path collision
- Replay verifies every binding before `EffectSucceeded` without invoking Codex or
  reopening a terminal carrier (terminal carriers reconstruct without `RESUME`)
- Adjudication delivers the full hash-verified sanitized observation snapshot on
  stdin; drift/size fail closed
- After reading Codex result JSON, publication and adjudication runners enforce
  shipped schemas locally (`extra="forbid"`, strict typed fields, no `str()`
  coercion of numbers/arrays/objects). Publication requires all four schema
  fields (`title`, `body`, `commit_subject`, `commit_body`) with no omission
  defaults; unknown properties and wrong types fail with privacy-safe errors
- Event/SQLite adjudication `safe_summary` values are fixed operational strings;
  free-form model text remains only in protected adjudication artifacts
- Source-run create and existing-PR prepare reject non-SSH / malformed remotes via
  `extract_remote_nwo` before protected artifacts or `PreparedState` are committed

## Simulated evidence (honest)

Automated suites use injected fakes only (no real network, GitHub, Cursor, Codex,
model, commit, push, or PR mutation).

Covered:

- config omit/disabled/invalid/secret/lease-heartbeat
- protected artifact roundtrip / hash / symlink fail-closed
- create-or-reuse identity + active ownership conflict
- start gate + prepared-requires-start on resume
- bounded history
- LOCAL router refusal without executor; publication exact-session argv (no `--last`);
  effect-bound publication/adjudication/local-fix replay
- PAUSED local fix resumable vs FAILED non-resumable
- prepare hardening: source status allowlist, existing-PR head_repo/checkout/path;
  exact frozen plan/prompt copies; source-origin drift/missing fail-closed;
  HTTPS/malformed remote rejection at create/prepare
- CLI prepare single discovery/output call
- ownership refuse on starttime/executable mismatch for supervisor and children
- real Phase 16.2 boundary for LocalFixAdapter (fake Cursor/Codex; no monkeypatch of
  `run_local_review_fix`)
- **terminal-carrier crash window:** delete effect-bound v2 result after carrier
  finalization → replay reconstructs without `RESUME` / agent call when patch
  and review evidence are complete and hash-verified; absolute/traversal paths,
  symlinked parents, oversized artifacts, missing patch hash, incomplete/
  drifted review or patch evidence block reconstruction and do not persist
  accepted v2 results
- **normal acceptance patch verification:** post-finalization patch-byte mutation
  before `_map_result` fails closed and cannot persist an accepted v2 result;
  normal and terminal paths share `DEFAULT_MAX_PATCH_BYTES` with protected
  persistence and write-gateway consumption
- **abort during `RunLocalFixEffect`:** `ControlPlaneService.abort()` with long fake
  Cursor (carrier active-process) + Codex (owned child) and ownership mismatches
- adjudication snapshot content delivery + size/hash fail-closed
- content-addressed fix prompts/replies across two actionable cycles (no path collision)
- carrier seed uses non-default frozen sandbox/timeouts/output_format/force/trust
- carrier reopen binding drift fail-closed for each `CarrierSeed` category,
  including snapshot-path drift/traversal and same-commit other-branch checkout;
  null→created chat allowed for ExistingPr first cycle
- `ProcessCodexRunner` minimal env (sentinel secret excluded), oversized capture,
  timeout including unread large stdin, SIGTERM-refusal→KILL, oversized result file
- publication/adjudication local schema enforcement: extra keys, wrong field
  types, and each missing required publication field rejected (privacy-safe errors)
- privacy: operational adjudication summaries only in events
- supervisor waiting_retry bounded waits (fake clock)
- **Multi-round E2E** (`test_phase16_7_simulated_e2e.py`):
  - SourceRunOrigin: prepare → start → publication → PR → bot → adjudicate → local fix →
    fix publication → cycle 2 → verified no-findings → `completed`
  - ExistingPrOrigin: prepare → start (skip initial publication) → same correction path →
    cycle 2 no-findings → `completed`
  - **ExistingPrOrigin two actionable cycles:** null prepared chat → first accepted
    local-fix chat reused exactly on later carriers (`test_existing_pr_two_actionable_cycles_preserve_exact_chat`)
  - Asserts journal event kinds, write kinds, distinct push idempotency targets,
    distinct content-bound `commit_patch` effect IDs/idempotency keys, exact
    session argv, deterministic `prv2c-` carriers
- Stale claim fencing after lease replacement
- Abort skips unowned launcher/child metadata
- Existing-PR discoverer + prepare CLI path with fake `gh api` runner
- Correction suites: `test_phase16_7_correction_round2.py`,
  `test_phase16_7_correction_round3.py`,
  `test_phase16_7_correction_round4.py`,
  `test_phase16_7_correction_round5.py`,
  `test_phase16_7_correction_round6.py`
## Validation commands / results

Focused Phase 16.7 suites (representative):

```text
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q \
  tests/unit/test_pr_review_v2_config.py \
  tests/unit/pr_review_v2/test_protected_result_store.py \
  tests/unit/pr_review_v2/test_local_fix_paused_resumable.py \
  tests/unit/pr_review_v2/test_codex_local_runners.py \
  tests/unit/pr_review_v2/test_phase16_7_create_or_reuse.py \
  tests/unit/pr_review_v2/test_phase16_7_local_executor.py \
  tests/unit/pr_review_v2/test_phase16_7_simulated_e2e.py \
  tests/unit/pr_review_v2/test_existing_pr_discovery.py \
  tests/unit/pr_review_v2/test_phase16_7_correction_regressions.py \
  tests/unit/pr_review_v2/test_phase16_7_correction_round2.py \
  tests/unit/pr_review_v2/test_phase16_7_correction_round3.py \
  tests/unit/pr_review_v2/test_phase16_7_correction_round4.py \
  tests/unit/pr_review_v2/test_phase16_7_correction_round5.py \
  tests/unit/pr_review_v2/test_phase16_7_correction_round6.py
```

Cursor-reported plan validation block (the final independent reviewer did not
repeat the full suite, MkDocs build, or package build):

| Command | Result |
|---|---|
| Focused reducer/write/E2E suites | **217 passed** |
| `uv run pytest -q` (full) | **1618 passed** |
| `uv run ruff format --check .` | passed |
| `uv run ruff check .` | passed |
| `uv run mypy src` | passed (122 source files) |
| `uv run mkdocs build --strict` | passed |
| `uv build` | passed (`ai_dev_loop-0.1.0` sdist + wheel) |
| `git diff --check` | passed |

## Privacy / safety evidence

- Status/history DTOs omit prompts, patches, thread bodies, tokens, full session IDs,
  argv, PID/PGID, environments
- Codex runners raise privacy-safe errors only
- Abort refuses mismatched/unowned launcher and child metadata
- LOCAL effects still complete only through `complete_claim` fencing
- Publication runner binds to live `effect.bound_head_sha` (prepare-time
  `expected_head_sha` is origin baseline only — required for multi-cycle fix publication)

## Unresolved risks / Phase 16.8 live gaps

1. Full real-gateway multi-round E2E (current E2E scripts READ/MUTATING/RECONCILE outcomes;
   LOCAL uses real LocalEffectExecutor + FakeCodex; carrier boundary covered with fake CLIs)
2. Systematic fault injection (timeouts, late results after abort, two competing workers
   mid-LOCAL, claim expiry) at every LOCAL/control seam beyond the stale-claim case
3. Real controlled PR acceptance (user-approved) with exact session continuity
4. Live `gh api` prepare against a real open PR (automated tests use injected runners only)
5. ~~Domain note: `commit_patch` idempotency keys are cycle-scoped without operation target~~
   **Resolved (correction round 6+):** `commit_patch` identities are content-bound via
   `stable_effect_ids(..., target="<expected_parent_head>:<accepted_patch_sha256>")`.
   Same-commit retries/reconciliation keep the identity; initial vs fix commits in the
   same external cycle never share effect ID, idempotency key, or `ADL-Idempotency`
   trailer. Push identities remain SHA-targeted as before.

## Phase 16.8 entry conditions and execution charter

Phase 16.7 is a staged, simulatedly validated implementation; it is not live
accepted. The Phase 16.8 agent must read this handoff,
`pr-review-v2-restructure-context.md`, the Phase 16.7 plan, every mandatory
`.cursor/rules/` contract, and the current `pr_review_v2` source and tests before
proposing a subphase. Verify the staged baseline before editing; do not replace
this evidence with assumptions from legacy `pr-review`.

Work Phase 16.8 in explicit, independently reviewable slices:

1. Add systematic fault injection at each read, LOCAL, mutation, reconciliation,
   timer, lease, restart, and abort boundary. Cover timeout, network/rate-limit,
   stale completion after abort or lease loss, competing workers, and corrupted or
   drifted PR/SHA/patch/thread-set evidence.
2. Exercise restart at polling, adjudication, Cursor, local review, and publication
   boundaries. Each scenario must prove no duplicate side effect and one precise
   durable safe next action.
3. Preserve privacy/redaction, reducer purity, outbox/claim fencing, protected
   artifacts, explicit start authority, exact session/chat continuity, and v2/legacy
   isolation. Never use `--last`, infer a session/chat, or weaken a fail-closed
   check merely to continue a run.
4. Treat a real controlled PR as a separate, user-approved write gate. Before any
   live `gh`, Git, Cursor, or Codex action, obtain the exact target repository,
   branch/PR, permitted write scope, rollback/abort owner, and explicit approval.
   Do not merge, force-push, retarget, delete branches, or perform Phase 16.9
   cutover/legacy cleanup.
5. Finish only when the full suite and A/B regression barrier are green, the
   controlled live cycle succeeds, and every known crash window either reconciles
   idempotently or leaves a documented safe action. Record commands, results,
   manual actions, and residual risk in the next findings handoff.

## Lessons from the A/B correction sequence

The seven corrective reviews do **not** show an X-to-Y-to-X regression loop.
They reveal a layered implementation gap: a broad initial slice established the
state and public surface, then each review reached a deeper production boundary
that earlier fake-driven tests had not exercised. Examples progress from launcher
and preparation identity, to carrier finalization/replay, abort ownership, frozen
configuration/path binding, normal-path patch verification, and finally same-cycle
commit identity. Later fixes preserved the earlier contracts and narrowed the
remaining surface.

The unusually high finding count had three primary causes:

- Phase 16.7 introduced a large control plane and several cross-phase seams in one
  slice (supervisor, subprocesses, carrier, protected artifacts, CLI, config, and
  write/reconciliation integration). Correctness depends on their composition, not
  only on individual unit behavior.
- Initial tests used injected fakes at several production boundaries. That was
  appropriate for the no-live-call rule, but it hid issues such as actual carrier
  finalization, subprocess I/O/termination, and post-artifact crash windows until
  the staged reviewer followed the real boundary contracts.
- The acceptance invariants are deliberately fail-closed and temporal: identity,
  ownership, content hashes, exact sessions, and crash ordering. A fix can make a
  previously unreachable deeper invariant observable; this is progressive coverage,
  not evidence that the earlier fix was wrong.

For future phases, reduce correction rounds by requiring a contract matrix before
implementation: enumerate every effect with its authority, identity target,
immutable inputs, protected outputs, retry/reconcile rule, abort order, and crash
windows. Add at least one production-boundary test (process fake, real local
carrier/runtime, or persistent SQLite restart) per row rather than only a mocked
end-to-end test. Review the narrow safety-critical seams after each implementation
slice, run focused fault tests before broad feature work, and reserve the final A/B
review for cross-seam composition rather than first discovery of baseline runtime
contracts.

## Explicit non-claims

This handoff does **not** claim Phase 16.8 live acceptance, real network GitHub fault
injection, or cutover completion. Detached supervisor spawn/reuse and production
`FilesystemLocalCarrierRuntime` seeding are implemented and covered by automated
fakes; live workstation validation remains Phase 16.8.

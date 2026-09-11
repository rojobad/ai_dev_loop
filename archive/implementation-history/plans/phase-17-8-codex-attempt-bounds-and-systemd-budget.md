# Phase 17.8: Codex attempt bounds and systemd runtime budget

## Goal

Correct the two independent scheduler-attempt limits found by the Parish360 manual smoke run:

1. a Codex JSONL event stream just above 256 KiB is currently treated as a process timeout, terminating the review before its schema result is written;
2. systemd receives a generic one-hour runtime cap and its implicit `TimeoutStartSec`, rather than the frozen Cursor/Codex turn budget.

The corrected scheduler must preserve bounded sensitive artifacts, distinguish real timeouts from output truncation, permit a valid review result to complete after its event trace reaches its storage limit, and give each transient unit a runtime envelope derived from the frozen workflow timeout.

## Non-Goals

- Do not change the target-repository YAML schema or add a new public timeout flag.
- Do not retry, resume, recover, mutate, or abort the existing Parish360 run.
- Do not create, guess, or bind a replacement reviewer B.
- Do not weaken artifact-size bounds, expose sensitive output, or capture unbounded process output.
- Do not make a real Cursor/Codex call or enable/install a systemd timer.
- Do not modify global integrations, Codex hooks, session bridges, skills, or destructive Git behavior.

## Scope

- Make the bounded streaming-process result represent timeout and output truncation independently.
- Add an opt-in bounded-drain policy for scheduler Codex reviews: retain at most the declared artifact limit, then continue draining and discarding excess bytes until the child exits or its real timeout expires.
- Align the scheduler Codex JSONL capture limit with the existing 8 MiB Codex events-artifact limit; retain the separate stderr bound.
- Persist safe, bounded truncation facts in the existing protected Codex metadata/outcome artifacts and classify a truncated, unusable review as a specific Codex review block rather than a timeout/uncertain attempt.
- Propagate an explicit per-attempt execution timeout through `LaunchRequest` and use it to set the systemd unit runtime envelope.
- Update focused operations/configuration documentation and create a concise findings artifact for this corrective phase.

## Out of Scope

- Rewriting scheduler state, ledger schemas, migrations, recovery lineage, or loop iteration semantics.
- Changing the general Cursor or tool-update capture policy.
- Treating a partial JSONL trace as authority to infer a reviewer identity or review decision.
- Automatic handling of old blocked runs after installation of this fix.
- The post-fix real workstation smoke test and the operator decision whether to abort the already-blocked Parish360 run.

## Required Context

Read before editing:

- `AGENTS.md`;
- all applicable `.cursor/rules/*.mdc`, particularly Codex-review, orchestrator, state/schema, loop/resume, abort, and governance contracts;
- `archive/implementation-history/master-plan.md`;
- `archive/implementation-history/plans/phase-17-5-codex-review-and-bounded-scheduler-loop.md`;
- `archive/implementation-history/plans/phase-17-6-abort-recovery-and-operations.md`;
- `archive/implementation-history/plans/phase-17-7-clean-cutover-and-acceptance.md`;
- `archive/implementation-history/findings/phase-17-precutover-acceptance.md`;
- `src/ai_dev_loop/process.py`;
- `src/ai_dev_loop/scheduler/codex_attempt_runner.py`;
- `src/ai_dev_loop/scheduler/application/attempt_backend.py`;
- `src/ai_dev_loop/scheduler/application/attempt_service.py`;
- `src/ai_dev_loop/scheduler/application/systemd_backend.py`;
- `src/ai_dev_loop/scheduler/application/codex_workflow_service.py`;
- `tests/unit/test_process.py`, `tests/unit/scheduler/test_attempt_executor.py`, `tests/unit/scheduler/test_phase17_5_codex_corrections.py`, and `tests/integration/test_phase17_5_scheduler_review_loop.py`;
- `docs/operacion/troubleshooting.md` and `docs/referencia/configuracion.md`.

The private manual diagnostic is authoritative only for this defect report: the Codex worker ended after about 77 seconds because a 260,698-byte events stream crossed the 256 KiB capture limit. The worker reported it as `timed_out: true`, returned 124, and therefore did not finish its review-result artifact. The frozen Codex timeout itself was 90 minutes. Systemd also showed an implicit 90-second `TimeoutStartSec` and a generic one-hour `RuntimeMaxSec`. Do not reproduce the real run or copy its private artifacts into the repository.

## Cursor Rules And Skills

- Follow all repository `.cursor/rules/*.mdc` contracts. In particular, use fake `agent`/`codex` executables in every automated test, keep process-group timeout/abort behavior intact, and never use `--last`.
- Read and follow the local `review-staged-ai-dev-loop-execution` skill only for the independent staged review after this implementation. Cursor must not self-review by changing the staged diff after review begins.
- This plan updates operational documentation, so use the docs-acceptance contract as evidence discipline: document only tested behavior and keep prompts, raw JSONL, full session IDs, and private artifact content out of docs/findings/default output.
- Do not stage, commit, push, reset, clean, stash, unstage, or make external Git changes.

## Architecture Guardrails

- `timed_out` means only that the configured process deadline expired. Output storage saturation is a distinct condition; it must never be converted into a synthetic timeout or exit code 124.
- Every process output stream remains bounded. The Codex review opt-in may drain excess bytes solely to prevent pipe backpressure; it must neither retain those bytes in memory nor write them to a file.
- Preserve existing default bounded-process behavior for non-Codex callers. The drain-after-limit behavior must be an explicit opt-in used only by the scheduler Codex review runner in this phase.
- The Codex events artifact must be capped at the already-declared `MAX_CODEX_EVENTS_ARTIFACT_BYTES` (8 MiB). The capture, metadata, and test assertions must agree exactly on the same bound.
- A valid schema review result remains the sole decision source. A successful child with a truncated event trace may complete only when its separate result artifact validates and reviewer identity is unambiguous.
- If a bootstrap trace is truncated before a single exact B identity is established, fail closed as `codex_bootstrap_uncertain`; never infer an ID or launch another B. If identity is already proven but the review cannot be validated because output was truncated, block with a specific safe `codex_review_output_truncated`-style reason (choose one canonical documented spelling and use it consistently), not `attempt_uncertain`.
- Runtime authority comes from the frozen submitted workflow context, not from the worker environment, YAML rereads, a generic backend default, or an artifact that a child may modify.
- `LaunchRequest` is the typed boundary for the execution timeout. Validate it as a positive integer and update every real/fake backend fixture and test constructor deliberately; do not add a silent one-hour fallback.
- The systemd runtime envelope must be the requested execution timeout plus a named, bounded finalization grace. Set both `TimeoutStartSec` and `RuntimeMaxSec` explicitly to that envelope. Keep `TimeoutStopSec` explicit and bounded as a separate termination grace; preserve the existing abort-control ownership checks.
- Persist only safe truncation booleans/counts in protected attempt metadata; do not place raw output or full session IDs in ledger state, events, status, history, logs, CLI errors, docs, or findings.

## Implementation Plan

1. Refactor the bounded-stream capture contract in `process.py`.

   - Extend `StreamingProcessResult` with backward-compatible, explicit stream truncation flags (and, only if needed for audit, bounded captured-byte counts). Keep `timed_out` exclusively for deadline expiry.
   - Make `_BoundedTextCapture` retain no more than its byte limit even when a read chunk crosses that limit, preserve valid incremental UTF-8 decoding for retained bytes, and report truncation separately.
   - Add an explicit keyword-only drain-after-limit policy, defaulting to the current terminate-on-limit behavior for all current non-Codex callers. Under the opt-in policy, continue multiplexing/draining stdout/stderr after a stream is full, discard additional bytes, observe the same deadline and process-group cleanup path, and return the actual child exit status.
   - On default terminate-on-limit behavior, classify the result as an output limit condition (not `timed_out`) and preserve the existing bounded process-group termination safety. Audit and update every expectation that previously treated limit overflow as timeout 124.

2. Apply the policy narrowly to scheduler Codex attempt execution.

   - In `scheduler/domain/codex_contract.py`, make the JSONL capture bound and `MAX_CODEX_EVENTS_ARTIFACT_BYTES` one coherent 8 MiB contract (retain an independently justified stderr cap).
   - In `codex_attempt_runner.py`, enable drain-after-limit only for the Codex stdout/stderr call. Record safe `stdout_truncated`/`stderr_truncated` facts in the protected metadata and authenticated outcome; leave raw excess bytes unrecoverable by design.
   - Preserve normal completion when the Codex process exits successfully, the schema result artifact is within its existing bound and validates, and bootstrap/reviewer identity is fully proven. Do not make trace truncation itself an actionable review finding.
   - When truncation coincides with an absent/invalid review result, produce a canonical explicit failure/block classification consumed by `CodexWorkflowService`. For bootstrap identity absent/partial/conflicting under truncation, route through the existing fresh-B uncertainty boundary. For an already-bound reviewer, route through `CodexReviewBlockedEvent`. Both paths must preserve artifacts and must not retry or create B.
   - Keep nonzero exit, genuine timeout, malformed schema, and pre-execution guard behavior distinct and fail closed. Do not leak raw events, stderr, prompts, or full IDs through summaries.

3. Propagate frozen turn budgets to systemd.

   - Add `execution_timeout_seconds` to `LaunchRequest`. In `AttemptService._launch_recorded_attempt`, compute it from the loaded, validated submitted state: `cursor_timeout_minutes * 60` for Cursor attempts and `codex_timeout_minutes * 60` for Codex attempts. Use a positive explicit budget for the retained synthetic/fake path only where it is actually invoked.
   - Update `FakeAgentProcessBackend` and all fixture constructors to retain and assert the value without changing their deterministic lifecycle semantics.
   - Replace the generic `attempt_runtime_seconds` launch cap in `SystemdUserBackend` with a named bounded finalization grace added to the request budget. Emit `TimeoutStartSec` and `RuntimeMaxSec` with the same computed envelope, and a separate named bounded `TimeoutStopSec` grace. Validate the request/derived values before emitting `systemd-run` argv.
   - Do not infer a timeout from `systemd` defaults. Do not change unit ownership, retained-unit observation, flock, result-envelope, or abort termination behavior.

4. Document and hand off the corrected operational contract.

   - Update `docs/referencia/configuracion.md` to state that the frozen `cursor_timeout_minutes` and `codex_timeout_minutes` also form each scheduler unit's execution budget, plus bounded internal finalization grace; they are not overridden by a one-hour backend default.
   - Add a focused `docs/operacion/troubleshooting.md` entry covering a Codex event artifact that reached its bounded capture limit, what metadata/status can safely show, when an explicit Codex-review block occurs, and that a fresh run is required after an unresumable blocked review. Do not include command output, sensitive paths, or reviewer IDs.
   - Create `archive/implementation-history/findings/phase-17-8-codex-attempt-bounds.md` with the diagnosis in safe terms, changed contracts, commands/results, unperformed real-model work, and residual risk. State that the preexisting Parish360 run was not modified.

5. Leave the implementation staged for independent review.

   - Run the validation below. Then use the staged-change review skill against this plan, report any actionable findings, and make only verified corrections within this plan's scope before re-running affected checks.
   - Do not commit. A human must review the final staged diff because this is a scheduler control-plane/process-lifecycle change.

## Testing Criteria

- Process-unit tests prove: a genuine timeout still terminates/reaps the process group and sets only `timed_out`; default output-limit behavior terminates and marks truncation without claiming timeout; opt-in drain-after-limit preserves a bounded artifact, discards overflow, returns normal exit for a completing child, and still times out/reaps a child that keeps running after truncation. Cover stdout and stderr, a chunk crossing the exact limit, and multibyte UTF-8 boundaries.
- Scheduler Codex tests use only a fake executable that emits more than 8 MiB of JSONL before a valid schema result: verify events storage never exceeds the bound, metadata/outcome safely mark truncation, the exact bootstrap B is bound once, and the review reaches its normal decision. Add the counterpart where the result is absent/invalid after truncation and verify the canonical blocked reason, preserved artifacts, released capacity, and no replacement B.
- Regression tests prove a genuine Codex process timeout remains a timeout and a normal small-output Codex review has no truncation facts or altered review decision.
- `LaunchRequest`/attempt-service tests assert 90-minute Cursor and Codex workflows pass 5,400 seconds from frozen state, rather than a backend default. Systemd argv tests assert `TimeoutStartSec` and `RuntimeMaxSec` are that budget plus named grace, `TimeoutStopSec` is explicit/bounded, and no old generic `RuntimeMaxSec=3600` expectation remains.
- Run the relevant scheduler integration loop with fake CLIs and fake backend; ensure no status/history/default-output surface leaks trace contents, prompts, raw stderr, or full reviewer identity.

## Validation

Run focused tests first, then the full suite and quality gates:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/test_process.py \
  tests/unit/scheduler/test_attempt_executor.py \
  tests/unit/scheduler/test_phase17_5_codex_corrections.py \
  tests/integration/test_phase17_5_scheduler_review_loop.py \
  tests/integration/test_phase17_6_abort_lifecycle.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m build
uv run mkdocs build --strict
git diff --check
```

## Risks Or Recovery Notes

- This fixes product behavior for future attempts only. The existing Parish360 run retains a real fresh-B side effect and incomplete review evidence; it cannot be manually resumed or have a second reviewer created. After this change is independently accepted and locally installed, an operator must explicitly decide whether to non-destructively abort that run and submit a fresh one. Its staged target-repository changes remain untouched by abort.
- The real smoke showed that a systemd 90-second start default exists, but its 77-second worker exit was caused by the product capture cap. The implementation must correct both conditions and must not misattribute future real deadlines.
- Draining output after the cap trades full trace retention for process liveness while retaining a hard artifact bound. Valid schema output and exact reviewer identity remain mandatory; otherwise the run blocks safely.
- Do not execute real model/systemd acceptance in this phase. A post-merge operator smoke should use a disposable target repo, a fresh reviewer B, and inspect only safe status/history summaries.

## OpenQuestions

None. The corrective behavior, fixed bounds, and per-attempt timeout authority are defined above; real-run recovery remains an explicit operator action outside this implementation.

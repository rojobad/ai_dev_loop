# Phase 16.8 Gate B Carrier Cursor Usage-Limit Recovery

## Goals

- Turn a classified Cursor usage-limit failure inside the deterministic Phase
  16.2 subordinate carrier into a privacy-safe, explicitly resumable
  `pr-review-v2` checkpoint.
- After a later explicit `pr-review-v2 resume`, recover the failed carrier as
  an immutable usage-limit successor using its frozen Cursor model, preserving
  the exact Cursor chat and Codex session, then continue the same v2
  `RunLocalFixEffect` without duplicate effects or writes.
- Keep all unclassified, corrupt, drifted, or ambiguous carrier failures fail
  closed.

The observed regression is a Gate B run that completed observation and
adjudication, then had its local carrier exit with durable
`cursor_usage_limit` evidence. The v2 worker converted that typed failure to
generic `local effect failed`, leaving the parent paused but unable to resume
the terminal failed carrier. No commit, push, PR update, thread mutation, or
local repository change followed the local effect start.

## Non-Goals

- Do not change Cursor account capacity, add model configuration, choose a
  substitute model, or automatically retry while capacity remains exhausted.
- Do not add a public `pr-review-v2 recover` command.
- Do not couple v2 to legacy `pr-review`, change legacy `recover` CLI behavior,
  or alter GitHub polling, no-findings, adjudication, publication, mutation,
  retry, or Phase 16.1 A/B semantics.
- Do not complete Gate B, modify a live run, or classify by parsing generic
  `last_error` text.

## Scope

Expected production seams:

- `src/ai_dev_loop/pr_review_v2/infrastructure/local_fix_adapter.py`
- `src/ai_dev_loop/pr_review_v2_carrier.py`
- `src/ai_dev_loop/pr_review_v2/workers/local_executor.py`
- `src/ai_dev_loop/pr_review_v2/application/control.py` only if active carrier
  resolution for abort must follow a recovered descendant.
- Existing Phase 13 recovery code only behind a narrow infrastructure boundary;
  do not change its public CLI contract unless required by a test.
- The current v2 CLI/troubleshooting/observability docs only for honest status
  and resume guidance.

Use the existing local-fix executor/paused-resume unit tests and Phase 16.8
real-carrier restart, control/abort, privacy, supervisor and existing-PR
happy-path suites. Add a focused module only if those files cannot express the
production-boundary scenario clearly.

## Out of Scope

- Live Gate B runs, PRs, branches, checkouts, XDG state, artifacts, SQLite DB,
  GitHub/Cursor/Codex/model/network/credential/SSH-agent access.
- `ai_dev_loop.yaml`, public configuration, model catalogs, retry limits, and
  test fault knobs in public config.
- Manual state/artifact/prompt/session/chat edits or any Git/GitHub mutation.
- Commit, push, install, start, launch, resume, abort, reset, clean, stash,
  checkout, rebase, merge, or other Git-state change.

## Required Context

Read before editing:

1. This plan and `archive/implementation-history/plans/phase-16-8-pr-review-v2-resilience-and-live-acceptance.md`.
2. `archive/implementation-history/findings/phase-16-7-to-16-8-handoff.md`.
3. Recent Gate B correction plans for supervisor/lease, SSH agent, adjudication
   and existing-PR preimage adoption.
4. `local_review_loop.py`, `workflow_engine.py`, `commands/recover.py`,
   `recovery_planner.py`, `runners/cursor_failure.py`, and Phase 13 tests.
5. The v2 local adapter/carrier/local executor/control/supervisor/owned-child
   implementation, state/effect/reducer contracts, and their current tests.
6. `docs/operacion/prepare-start-resume-abort.md`, troubleshooting,
   observability and CLI reference, plus `TECHNICAL_DEBT_PHASE_16_8.md`.
7. Every applicable `.cursor/rules/*.mdc` file.

Use only these sanitized facts. Do not inspect the live run, acceptance
checkout, carrier artifacts, or a real PR.

## Cursor Rules And Skills

Read and obey all mandatory repository rules:

- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`

No repository `AGENTS.md` or `.cursor/skills/` exists at plan time. If either
appears, discover and obey it before editing. Do not invoke the staged-review
skill; the external A/B process invokes it after staging.

## Architecture Guardrails

1. **Classified evidence only.** Enter this path only from protected,
   contemporaneous failed-carrier Cursor metadata with
   `failure_code == cursor_usage_limit`, a valid fingerprint, and the existing
   Phase 13 recovery analysis. Never infer it from `last_error`, stderr, or a
   generic exit code.
2. **Explicit authority.** The first classified failure blocks the parent in a
   resumable local-fix state with `SafeActionKind.RESUME_SAME_EFFECT`; it must
   not create a successor or timer retry by itself. A later explicit v2 resume
   is the only authority that can create or reuse a recovery carrier.
3. **Immutable lineage.** Leave the failed source carrier terminal. Reuse the
   Phase 13 transactional/idempotent successor mechanism and use the frozen
   `ExecutionContextArtifact.cursor.model` verbatim, including `auto`; never
   prompt, discover, or substitute a model.
4. **Exact identity.** Preserve the exact Cursor chat and prepared Codex
   session. Never use `--last`, create a replacement chat after progress, or
   guess a session/chat. The deterministic carrier remains the source binding;
   a verified successor may be the active runtime carrier and accepted-result
   or abort target.
5. **One effect path and fencing.** Parent state still changes only through
   `EffectWorker` and `complete_claim`. Resume restores the same effect. Late
   results, stale leases, abort, and failed recovery cannot publish, commit,
   push, update PR text, or complete a stale claim.
6. **Fail closed.** Invalid/missing/unsafe fingerprint, non-usage failure,
   recovery blocker, lineage ambiguity, or repository/branch/HEAD/prompt/hash/
   chat/session drift must leave an inspectable blocked state without agents or
   external writes.
7. **Abort target.** Once a successor is active, abort must persist first and
   signal only its verified owned children; it must never signal unrelated
   processes or assume the failed source carrier owns live children.
8. **Privacy.** Do not leak raw agent output, prompts, patches, fingerprints,
   artifact paths, chat/session IDs, or provider messages into parent SQLite,
   status/history/logs or default CLI output.
9. **No legacy coupling.** Reuse generic Phase 13 local recovery behind a
   narrow infrastructure interface only; v2 must not depend on legacy PR flow.

## Implementation Plan

### 1. Type and map the first failure correctly

- Add a narrow privacy-safe local-adapter exception/result for a verified
  carrier usage-limit condition. It must contain no raw child data.
- Preserve this type when the real `run_local_review_fix()` boundary raises
  Phase 13 `CursorUsageLimitError`; retain current fail-closed behavior for all
  other failures.
- In `LocalEffectExecutor`, map it to `EffectBlocked` with a fixed safe
  summary, `PauseReasonKind.LOCAL_FIX_PAUSED`, and
  `SafeActionKind.RESUME_SAME_EFFECT`. Do not map it to a timer retry or generic
  `local effect failed`.
- Preserve all existing Codex validation/artifact/error mappings.

### 2. Recover only under a later v2 resume

- Add a narrowly named `LocalCarrierRuntime` operation that inspects a
  deterministic carrier and, only when the Phase 13 analysis proves its cursor
  usage-limit checkpoint, creates or idempotently reuses the recovery successor
  for the supplied frozen model. It returns the verified effective carrier ID.
- Implement it in `FilesystemLocalCarrierRuntime` through the existing recovery
  application boundary; never copy/rewrite carrier state or artifacts.
- On a later parent-resumed invocation, resolve the successor, revalidate the
  full `CarrierSeed`, fence only its owned orphaned children, then call
  `run_local_review_fix()` with the operation matching its durable checkpoint.
- Support an idempotently reused nonterminal successor and a failed-successor
  chain through the existing planner; never create siblings or guess paths.
- Keep the effective carrier ID out of ad-hoc parent state. Persist it only in
  normal protected local-fix evidence after terminal accepted success.

### 3. Keep restart, control, abort and docs coherent

- Preserve current v2 resume behavior: it restores the same `RunLocalFixEffect`
  and starts one supervisor; carrier recovery happens under that claim, not in
  status, preparation, history, or a timer.
- Make active carrier discovery for abort follow a validated current successor
  if necessary, preserving persist-before-signal, PGID/starttime/executable
  validation, and ambiguity refusal.
- Preserve terminal accepted-carrier reconstruction without re-running agents.
- Update docs/status only to state that capacity exhaustion needs a later normal
  v2 resume and that no automatic retry or PR mutation occurs first. Do not
  claim Gate B passed.

### 4. Automated acceptance

Use fake `agent` and fake `codex` executables, native isolated XDG state,
temporary Git repositories, real `FilesystemLocalCarrierRuntime`, real
`run_local_review_fix()`, and real v2 SQLite/worker/control/recovery boundaries.
Do not mock away the carrier boundary or invoke real models/network tools.

Prove all of the following:

1. A classified failure yields resumable parent pause and `RESUME_SAME_EFFECT`,
   not generic `local effect failed`, no retry timer, and no Git/GitHub write.
2. The failed source remains immutable, includes valid protected evidence, and
   raw fake child output never appears on safe parent surfaces.
3. An explicit parent resume creates/reuses exactly one successor with frozen
   `auto` (and a non-`auto` fixture if the existing test seam supports it), the
   exact prior chat, and exact Codex session; completed work is not rerun.
4. The recovered carrier can complete through the real boundary. SQLite/
   supervisor reopen before `complete_claim` neither duplicates the successor,
   Cursor/Codex calls or local-fix result nor performs downstream writes twice.
5. A second usage-limit failure advances only one verified successor chain on a
   later explicit resume; it never produces parallel successors.
6. Invalid fingerprint, non-usage failure, source/successor binding drift,
   unsafe artifact, recovery blocker and ambiguous descendant fail closed before
   any agent/model/GitHub/Git mutation.
7. Abort while the recovered carrier owns a blocking fake child persists abort
   first, signals only that descendant, preserves repository contents, and
   fences late completion.
8. Terminal replay, ordinary local-fix failures, legacy usage-limit recovery,
   v1 PR review, recent Gate B fixes, and the Phase 16.1 A/B barrier stay green.

## Testing Criteria

- Add unit/contract tests for typed mapping, recovery eligibility, safe status
  guidance, and persistence/serialization only where an existing typed contract
  changes.
- Add production-boundary integration tests with real carrier/recovery and
  SQLite reopen; a monkeypatched `run_local_review_fix` exception or metadata
  assertion alone is insufficient.
- Assert exact counts of carrier successors, Cursor/Codex calls, local-fix
  results, claims/completions and Git/GitHub writes.
- Use no live XDG paths, Parish360 checkout, credentials, Cursor/Codex models,
  or network activity.

## Validation

Run focused tests first, then at minimum:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q tests/unit/pr_review_v2
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q tests/integration/test_phase16_8_local_restart.py tests/integration/test_phase16_8_control_matrix.py tests/integration/test_phase16_8_supervisor_entry.py tests/integration/test_phase16_8_privacy.py tests/integration/test_phase16_8_existing_pr_happy_path.py tests/integration/test_phase16_1_ab_regression_barrier.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest --collect-only -q
uv run mkdocs build --strict
uv build
git diff --check
```

If unrelated global formatting drift remains, do not change it solely for a
pass; report it separately and format every changed file.

## Risks Or Recovery Notes

- This correction does not authorize resuming the live run. First complete
  staged A/B review, commit/push and CLI installation. Then controller A may
  issue exactly one explicit v2 resume and one bounded status check.
- A subsequent capacity failure must pause again. It is never authority to
  retry/reconcile a remote write.
- If recovery cannot prove the carrier checkpoint, preserve the paused parent;
  manually editing state is forbidden.
- Gate B remains controller/operator work after review. Cursor must not access
  or mutate it.

## OpenQuestions

None.

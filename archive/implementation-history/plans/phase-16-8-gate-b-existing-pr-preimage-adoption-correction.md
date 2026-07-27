# Phase 16.8 Gate B Existing-PR Preimage Adoption Correction

## Goals

- Make `pr-review-v2 prepare` support the Phase 16.8 existing-PR workflow
  safely: capture an immutable, protected, content-bound preimage of the
  already-open PR title/body during read-only preparation.
- Permit the first v2 PR-text update for that prepared existing PR only when
  the live title/body still match the captured preimage exactly; after a
  successful update, retain the existing v2 owned-marker semantics for later
  updates and reconciliation.
- Preserve fail-closed behavior for human edits, foreign/ambiguous markers,
  corrupt/missing protected artifacts, and all runs prepared before this new
  preimage binding existed.
- Make the intended Gate B existing-PR acceptance path executable without
  manually adding an `adl-v2` marker to a user-created PR.

## Non-Goals

- Do not weaken the existing owned-marker verification or let v2 overwrite a
  PR whose current title/body differs from its frozen adopted preimage.
- Do not synthesize or restore a preimage from current GitHub text at resume,
  retry, reconciliation, or update time.
- Do not migrate, patch, or otherwise recover the already-paused Gate B run;
  it lacks the immutable preimage required by this change and must remain
  fail-closed.
- Do not redesign the state machine, mutating outbox, claim/lease fencing,
  bot-review detection, Cursor/Codex execution, Git publication, or retries.
- Do not automatically create a PR, alter user PR text at `prepare`, or make
  `prepare` externally mutating.
- Do not finish Gate B or Phase 16.8 in this correction.

## Scope

Expected production scope:

- `src/ai_dev_loop/pr_review_v2/infrastructure/existing_pr_discovery.py`
- `src/ai_dev_loop/commands/pr_review_v2.py`
- `src/ai_dev_loop/pr_review_v2/application/preparation.py`
- `src/ai_dev_loop/pr_review_v2/application/execution_context.py` and/or a
  dedicated protected-input contract module, only for an immutable preimage
  reference and its validated payload.
- `src/ai_dev_loop/pr_review_v2/domain/common.py`, domain state/effects, and
  reducer only as necessary to carry the immutable preimage reference into the
  first `update_pr_text` effect.
- `src/ai_dev_loop/pr_review_v2/infrastructure/protected_result_store.py` and
  `input_artifacts.py` for owner-only, no-follow, regular-file, size-, hash-,
  and binding-verified persistence/readback.
- `src/ai_dev_loop/pr_review_v2/infrastructure/github_write_gateway.py` and
  directly related write contracts/reconciliation only.

Expected tests:

- existing-PR discovery/preparation unit tests;
- `tests/unit/pr_review_v2/test_github_write_gateway.py` or its current
  equivalent;
- existing Phase 16.8 existing-PR happy-path, corruption, restart, privacy,
  mutating/reconciliation, and A/B regression suites;
- narrow production-boundary helpers/fakes only when the current ones cannot
  carry a title/body preimage safely.

Documentation changes are required only if current v2 CLI/troubleshooting text
claims every arbitrary existing PR can be text-updated without explaining the
frozen-preimage requirement.

## Out of Scope

- The live acceptance checkout, GitHub PR, branches, comments, commits,
  threads, reviews, test fixtures, XDG state, protected run artifacts, and
  paused Gate B run.
- Any real GitHub, `gh`, Git remote, Cursor, Codex, model, network, agent, or
  user environment access.
- Manual artifact/state/SQLite edits, manual marker insertion, manual PR text
  restoration, or any command that resumes, starts, aborts, or recreates Gate
  B.
- `ai_dev_loop.yaml`, public project configuration, model selection, retry
  limits, and legacy `pr-review` behavior.
- Commit, push, install, branch, checkout, reset, clean, stash, merge, or
  rebase operations.

## Required Context

Read before implementation:

1. This complete plan.
2. `archive/implementation-history/plans/phase-16-8-pr-review-v2-resilience-and-live-acceptance.md`, especially Gate B §§13.2–13.3.
3. `archive/implementation-history/plans/phase-16-8-gate-b-effective-ssh-agent-correction.md`.
4. `src/ai_dev_loop/commands/pr_review_v2.py` (`prepare_existing_pr`).
5. `src/ai_dev_loop/pr_review_v2/infrastructure/existing_pr_discovery.py`.
6. `src/ai_dev_loop/pr_review_v2/application/preparation.py` and execution-context contracts.
7. `src/ai_dev_loop/pr_review_v2/application/write_contracts.py`:
   `ContentBoundMarker`, `parse_single_owned_preimage`, and
   `verify_owned_preimage_content`.
8. `src/ai_dev_loop/pr_review_v2/infrastructure/github_write_gateway.py`,
   including `_update_pr_text`, `_reconcile_update_pr_text`, and
   `_eligible_pr_preimage`.
9. Protected-result/input-artifact readers and the current existing-PR
   discovery, write, corruption, privacy, and restart tests.
10. Every applicable `.cursor/rules/*.mdc` file.

Sanitized failure evidence defining this regression:

- Gate B correctly used a newly created, user-owned existing PR as required by
  the Phase 16.8 plan.
- The PR was not created by v2, so its initial title/body did not carry an
  `adl-v2` ownership marker.
- After the local correction was committed and pushed, v2 attempted the first
  `update_pr_text` and blocked with the safe summary that the bound PR lacked
  an intact owned preimage.
- The current implementation captures only identity/head binding at
  `prepare`; it captures neither the original title/body nor a protected hash
  binding that could authorize an initial safe overwrite.
- This behavior prevents unsafe overwrites, but makes the documented
  existing-PR happy path unable to complete whenever a local fix needs to
  update PR text.

Do not inspect the live run, its artifacts, the test PR, or any real PR text.

## Cursor Rules And Skills

Read and obey every applicable rule:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

No repository-local Cursor skill is required. If the existing state/artifact
contract cannot represent one-shot adopted-preimage authority without changing
the safety model, stop and report an `OpenQuestions` item before dependent
changes.

## Architecture Guardrails

- The external PR at `prepare` is read-only input. Capture title/body only via
  the bound, same-repository open-PR discovery response; do not fetch an
  arbitrary URL or accept text from CLI/config/model output.
- Persist the adopted preimage as a dedicated protected artifact, not in plain
  status/history/logs or an unprotected project file. It must have bounded
  title/body sizes, canonical serialization, owner-only permissions,
  no-follow/regular-file reads, run-relative confinement, and hash verification.
- Bind the artifact to repository, PR number, head/base branch, and the
  prepare-time head SHA. Carry only its immutable `ArtifactRef`/hash through
  state and effects.
- `prepare` remains external-write-free. It may persist the protected input and
  durable prepared state only after all discovery, binding, and artifact
  validation succeeds.
- The initial adopted preimage authorizes exactly one class of overwrite: the
  first `update_pr_text` for the matching `existing_pr` origin, when the live
  PR title and body are byte/canonical-value equal to the frozen values and the
  current PR binding is otherwise valid.
- Preserve an explicit one-shot authority boundary. Once v2 has durably
  confirmed its first PR-text update, later updates/reconciliation require the
  existing exact v2 content-bound marker; a removed/changed marker must never
  regain authority merely because text happens to resemble the old preimage.
- Any title/body drift, duplicate/malformed/foreign marker, missing/corrupt/
  oversized/wrong-binding preimage, ambiguous read, or old run without the new
  ref must block or reconcile as `UNRESOLVED` before a write. Never refresh the
  preimage on resume/retry/reconciliation and never fall back to current text.
- `APPLIED`, `PROVEN_NOT_APPLIED`, and `UNRESOLVED` reconciliation remain
  distinct. A crash after the initial text write must prove exact marker/content
  before declaring applied; a still-exact adopted preimage may prove not
  applied only while one-shot adoption authority has not been consumed.
- Existing source-run behavior and already-owned PRs keep the current marker
  path unchanged. Backward compatibility is fail-closed: existing runs and
  old protected contexts with no adopted-preimage ref cannot be auto-adopted.
- Do not expose title/body, their raw hashes when they reveal content, markers,
  full session IDs, prompts, patches, or artifact paths in status, history,
  exceptions, fake operational output, or docs.
- Use production-boundary fakes and temporary XDG/artifact roots only. Do not
  add test fault knobs to public config. Cursor must not modify Git state.

## Implementation Plan

### 1. Capture a bounded, protected existing-PR preimage during preparation

- Extend existing-PR discovery's one read-only GitHub response to include the
  title and body required for an initial preimage. Preserve all current state,
  number, same-repository head, branch, and SHA checks.
- Define a typed adopted-preimage artifact with title/body and binding metadata.
  Validate text bounds/control characters/canonicalization consistently with
  current publication-text contracts; allow an empty body only if GitHub PR
  semantics already do.
- Carry that typed value through `ExistingPrSnapshot`, persist it under the
  new run before the durable `PreparedState`, and place only its protected,
  hash-verified reference in `ExistingPrOrigin` or an equivalently immutable
  origin-bound contract.
- Make repeated `prepare` with the identical bound PR/preimage idempotently
  reuse the exact run/artifact. A mismatch must fail closed rather than replace
  a preimage under an existing run ID.

### 2. Bind initial update authority explicitly and one-shot

- Extend only the first existing-PR `UpdatePrTextEffect` path with the adopted
  preimage reference and a durable one-shot authority indicator. The reducer
  must derive this from immutable origin/state, never from a runtime flag or
  current GitHub text.
- Maintain the existing `ArtifactRef`/effect equality/state validation rules;
  update schemas/models/fixtures deliberately with backward-compatible defaults
  only where old runs must load.
- Ensure source-run and already-owned PR update effects retain their current
  contract and do not acquire an adopted-preimage capability.

### 3. Authorize first update and reconciliation safely

- In `GitHubWriteGateway`, read the preimage through the protected input reader
  and verify its run/binding/hash before authorization.
- For the one eligible initial existing-PR update, allow mutation only if the
  fetched title/body exactly equal the frozen preimage and there is no
  conflicting/ambiguous ownership evidence. Then write the normal v2
  content-bound marker and confirm it exactly.
- Preserve the current marker-only behavior after that initial confirmed write.
- Update reconciliation so:
  - exact intended v2 marker/content proves `APPLIED`;
  - exact unconsumed adopted preimage can prove `PROVEN_NOT_APPLIED`;
  - any drift, missing artifact, marker ambiguity, consumed-adoption mismatch,
    or incomplete proof yields `UNRESOLVED` / fail-closed, never a fresh write.
- Do not make the generic create-or-update PR path overwrite foreign PR text.

### 4. Preserve recovery, privacy, and legacy safety

- Add loading behavior for old state/context/origin shapes that retains their
  prior block: no retroactive preimage extraction or adoption.
- Make artifact errors typed and privacy-safe; they must not leak original PR
  text through event details, retry summaries, status/history, or test logs.
- Verify a restart after preimage persistence keeps the same ref/hash and that
  no retry/reopen can substitute a changed remote PR body.
- Update user-facing docs only if required to explain that a fresh `prepare`
  freezes the existing PR preimage and that an operator must create a new run
  after a failed legacy run; do not claim Gate B passed.

### 5. Automated acceptance

Add production-boundary tests proving:

1. `prepare` reads and persists an exact bound existing-PR title/body without
   external mutation; repeat prepare reuses it and mismatch fails closed.
2. A normal unmarked user-created PR can execute its one initial text update
   only when live title/body exactly match the protected adopted preimage.
3. The update writes/validates the normal v2 marker, and subsequent updates use
   marker authority rather than the adopted snapshot.
4. Title-only, body-only, whitespace/canonicalization, marker,
   head/repository/branch, and duplicate-PR drift block before any mutation.
5. Missing, wrong-run, wrong-binding, tampered, unsafe-mode, oversized,
   symlink/non-regular, or hash-invalid preimage artifacts fail closed.
6. SQLite close/reopen and crash-after-write reconciliation distinguish
   `APPLIED`, `PROVEN_NOT_APPLIED`, and `UNRESOLVED` without duplicate PR-text
   writes.
7. Old prepared states without a ref remain blocked and cannot be converted by
   resume/retry.
8. Source-run and pre-owned-marker behavior remain unchanged.
9. Status/history/operational surfaces contain no preimage title/body, markers,
   raw artifact paths, or sensitive metadata.
10. Phase 16.1 A/B barrier and existing Phase 16.2–16.8 regressions remain
    green.

## Validation

Run the focused preparation/discovery/write/reconciliation/corruption/privacy
tests first, then at minimum:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q tests/unit/pr_review_v2
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q tests/integration/test_phase16_8_existing_pr_happy_path*.py tests/integration/test_phase16_8_corruption_matrix.py tests/integration/test_phase16_8_privacy.py tests/integration/test_phase16_1_ab_regression_barrier.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest --collect-only -q
uv run mkdocs build --strict
uv build
git diff --check
```

If existing filenames differ, use the closest current suites and state the
exact commands/results. Do not fix unrelated formatting drift merely to make a
global formatter pass.

## Risks Or Recovery Notes

- The paused Gate B run was prepared before this capability existed; it is not
  a candidate for artifact repair or automatic migration. Keep it paused or
  abort it only through the controller's documented durable action when the
  user decides to retire it.
- After staged A/B review, commit/push, and CLI installation, run a fresh
  `prepare` on a clean newly created acceptance PR (recommended) or a verified
  unchanged existing PR. This fresh prepare is required to capture the new
  preimage. Do not reuse the current run.
- A new manual PR without this code correction would reproduce the defect;
  creating a new PR is not a workaround for the missing binding.
- The fresh Gate B run must still follow its normal detached `start`, then
  bounded status/history and documented resume/abort controls only. No manual
  marker or preimage edits are permitted.

## OpenQuestions

None.

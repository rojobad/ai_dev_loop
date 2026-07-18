# Phase 15.5 — Independent PR-Review Preparation and Cursor Model Selection

## Goal

Extend Phase 15 so the autonomous GitHub PR-review loop can be prepared against
an **already-open PR**, even when that PR did not originate in `ai_dev_loop`.
The prepared independent cycle must bind to an explicit local repository branch,
PR number, and PR-head SHA; use the exact Codex reviewer session supplied by the
user; and create a new Cursor chat only if actionable external feedback actually
requires a correction.

Add an explicit, audited command to change the **Cursor** model before the first
Cursor turn of that independent PR-review cycle. This covers the intended case
where the preceding work used `auto` due to quota pressure and a preferred Cursor
model is available again.

## Non-Goals

- Do not weaken or replace the existing `pr-review create <source-run-id>` path.
  It remains the Phase 15 path for accepted staged work and continues to preserve
  its original Cursor chat.
- Do not create a PR in the new independent mode. The target PR must already
  exist and be explicitly identified.
- Do not infer a PR by recency, infer a branch, use `--last`, guess a Codex
  session, or accept a head SHA different from the checked-out local branch.
- Do not add webhooks, hosted workers, force push, merge, PR retargeting, PR
  closure, or credential storage.
- Do not switch the Codex reviewer model/session in this phase. The reviewer
  session and its captured effective review runtime remain frozen at independent
  prepare time; a different reviewer model requires preparing a new cycle from a
  Codex session that uses that model.

## Scope

- Add `ai_dev_loop pr-review prepare` for an independent, existing-PR binding.
- Support both identity arrangements already used by `prepare`:
  - **single-session:** `--codex-session-id <reviewer>`; after preparation the
    reviewer session must be inactive before the user starts the worker;
  - **A/B:** B passes `--codex-session-id <reviewer-B>` and
    `--controller-session-id <controller-A>`; B remains inactive and A starts
    the worker with its exact controller ID.
- Require the independent prepare input to include the target repository,
  explicit open PR number, explicit source branch, immutable plan path, prompt
  source path, and exact Codex session ID. Resolve remaining workflow/GitHub
  policy from the target repository's `ai_dev_loop.yaml`, with the existing
  explicit CLI overrides.
- Bind the cycle only after verifying: authenticated `gh`; configured GitHub
  policy enabled; local repository identity; clean worktree; checked-out branch
  equals the requested branch; local `HEAD` equals the requested PR's head SHA;
  PR is open; PR head branch/base match the command/config policy; and the PR is
  not cross-repository unless a later phase explicitly designs that support.
- Keep `cursor.chat_id` null at independent prepare. When every eligible bot
  finding is actionable, create exactly one new Cursor chat, persist it before
  its first turn, then reuse it for all later fixes in that run.
- Add `ai_dev_loop pr-review set-cursor-model <run-id> --cursor-model <model>`.
  It is valid only for a prepared/awaiting independent cycle that has no Cursor
  chat, no local correction iteration, no active worker, and no abort request.
  It runs the existing compatibility probe before persisting the change.

## Out of Scope

- Changes to target projects, their `ai_dev_loop.yaml` policy, SSH setup,
  GitHub authentication, or the Codex Desktop Pull Requests UI.
- General model switching for ordinary prepared runs or recovery runs; those
  retain their current contracts.
- Retrofitting a missing original plan/prompt from repository history or a
  Codex transcript. The user must provide both files at independent prepare.
- Automatically accepting a PR whose local checkout is behind/ahead of the
  exact remote PR head, dirty, detached, ambiguous, closed, or has a different
  base branch.

## Required Context

Read before implementation:

- `archive/implementation-history/master-plan.md`
- `archive/implementation-history/plans/phase-15-github-pr-review-loop.md`
- this Phase 15.5 plan
- `src/ai_dev_loop/commands/{prepare.py,pr_review.py,launch.py,controller.py}`
- `src/ai_dev_loop/{state.py,config.py,workflow_engine.py,resume_planner.py,cli.py}`
- `src/ai_dev_loop/runners/{github.py,codex.py,codex_github.py,cursor.py,probes.py,git.py,publish.py}`
- `src/ai_dev_loop/integrations/codex/{skill,controller_skill}/SKILL.md`
- Phase 15 unit/integration tests and fake `agent`, `codex`, and `gh` fixtures.

## Cursor Rules And Skills

Follow every `.cursor/rules/ai-dev-loop-*.mdc`, in particular:

- `ai-dev-loop-governance.mdc`
- `ai-dev-loop-orchestrator-contracts.mdc`
- `ai-dev-loop-state-and-schema-contracts.mdc`
- `ai-dev-loop-loop-and-resume-contracts.mdc`
- `ai-dev-loop-codex-review-contracts.mdc`
- `ai-dev-loop-abort-contracts.mdc`
- `ai-dev-loop-global-integrations-contracts.mdc`
- `ai-dev-loop-docs-acceptance-contracts.mdc`

Use `ai-dev-loop-docs-acceptance-governance` for docs/acceptance work. The
`create-cursor-plan` and staged-review skills are planning/review aids, not
runtime dependencies.

## Architecture Guardrails

- **Explicit adoption and exact binding:** independent mode must require an
  explicit PR number and branch. The typed GitHub adapter is the sole source for
  PR data. Refuse ambiguity, a closed PR, a non-`master` configured base, a
  cross-repository PR, head/branch mismatch, or any local Git drift. Record the
  verified PR URL/number/head/base and repository NWO as sensitive, typed
  run-state/artifact evidence; never select by latest/recent activity.
- **Identity continuity:** every external feedback adjudication and publication
  text turn resumes exactly `state.codex.session_id`. Never use `--last`, a new
  final-review session, an inferred session, or a subagent. In independent mode
  the first actionable feedback creates one Cursor chat; persist it atomically,
  then reuse exactly that chat for all correction turns and recovery.
- **Frozen model provenance:** the independent prepare captures the reviewer
  session runtime with `read_codex_session_runtime` and
  `resolve_effective_review_runtime`, just like normal prepare. `set-cursor-model`
  may replace only `state.cursor.model` before a chat exists; it must preserve
  a sensitive, append-only selection artifact with old/new value, source,
  timestamp, and probe outcome. It must not rewrite `source-config.yaml`,
  silently change Codex review model/reasoning, or mutate an active run.
- **State/audit compatibility:** make the PR-review origin explicit (for example
  `source_run` versus `independent_pr`) instead of using a fake source run ID.
  Preserve Phase 15 state readability and its source-run invariants. Version and
  validate any changed persisted model/schema; raw prompts, comments, output, and
  full IDs remain sensitive artifacts outside the target repository.
- **Controller boundary:** prepare is a non-mutating setup command with no worker
  start. For A/B, only the recorded controller A may run the new start command;
  B must remain inactive. For one session, the command must report that the
  reviewer must be inactive before start. Do not weaken controller identity
  checks simply because no source run exists.
- **GitHub/Git safety:** use `gh` only through `runners.github` argv adapters and
  use direct Git argv calls. No token/config logging, shell interpolation, force
  pushes, branch/base changes, merges, or writes until an explicit start command.
  Existing idempotent marker, thread eligibility, all-or-stop adjudication,
  commit/push verification, reply, resolve, abort, locks, and recovery guarantees
  apply unchanged after the binding is established.
- **No Cursor before a decision:** an independent PR can be polled and adjudicated
  without a Cursor chat. Only an all-actionable, validated result may create it.
  Any uncertain/not-applicable decision must reply `@rojobad ...`, leave every
  thread unresolved, and stop at `waiting_for_user_attention`.

## Implementation Plan

### 1. Specify the independent prepare/start command contract

1. Add typed `IndependentPrReviewPrepareOptions`/result types in the PR-review
   command layer. Expose a CLI shape equivalent to:

   ```bash
   ai_dev_loop pr-review prepare \
     --repo-path /path/to/repo \
     --pr 123 \
     --branch feature/example \
     --plan-path archive/.../plan.md \
     --prompt-source-path archive/.../prompt.txt \
     --codex-session-id <reviewer-session> \
     [--controller-session-id <controller-session>] \
     [--cursor-model <model>] [existing safe config overrides]
   ```

   The exact initial Cursor prompt arrives via stdin, matching normal `prepare`;
   preserve the source prompt file separately as normal `prepare` does.
2. Add `ai_dev_loop pr-review start <run-id>` for this origin only. In A/B mode
   require `--controller-session-id` and validate it against `state.controller`;
   in single-session mode emit/require the documented inactive-reviewer handoff.
   It must be an explicit write gate: preflight again, post exactly one idempotent
   `@codex review` marker at the persisted PR head, then spawn the existing
   detached worker.
3. Keep `pr-review create` and its output/semantics intact. Factor shared PR
   binding/worker-start logic rather than adding a second unsafe polling loop.
   Make `status`, `continue`, `resume`, and `abort` render/operate correctly for
   both origins, including safe origin/provenance labels without full IDs.
4. Extend the global controller/handoff skill only as needed: it must advertise
   independent prepare/start, enforce A/B roles, and never invoke it from B.
   Update command help and docs with single-session and A/B examples.

### 2. Build the independent binding and durable state layout

1. Reuse normal prepare's config resolution, clean-worktree validation, plan and
   prompt snapshots, effective/source config snapshots, `gh` auth, Codex runtime
   capture, model-runtime resolution, user-only permissions, manifests, and
   events. Do not call normal `prepare_run`: it has pre-implementation baseline
   and Cursor expectations that do not apply to an already-published PR.
2. Introduce a typed origin-aware PR-review binding, such as
   `GithubPrReviewState.origin = "source_run" | "independent_pr"`, with
   `source_run_id` required only for `source_run`. For independent binding store
   typed verified PR/repository evidence and an explicit initial-trigger lifecycle.
   Bump/version schema as necessary; old Phase 15 state remains valid and means
   `source_run` when its persisted representation lacks the new field.
3. Create an independent run in `prepared` state with `cursor.chat_id=None`, a
   fresh empty baseline-status artifact, immutable plan/prompt snapshots, a
   repository baseline equal to the verified PR head, and `github_pr_review`
   lifecycle such as `prepared_independent`. Do not create a Cursor chat, invoke
   Codex, post a GitHub comment, or mutate Git during prepare.
4. Under both repository/run locks, make repeated prepare calls deterministic:
   identify an existing nonterminal independent cycle for the same repository NWO,
   PR number, and bound SHA and return it rather than creating concurrent workers;
   reject a different live cycle on that worktree/PR. Persist safe errors/events
   after state has been created.

### 3. Start/poll an adopted PR safely

1. At `pr-review start`, re-read the PR and local repository, require every
   prepare-time identity predicate again, and reject any head/base/branch/working
   tree drift before writing a GitHub comment. Validate current `gh` auth, SSH
   publication readiness, configured reviewer allowlist, model tool compatibility,
   no abort request, and no live worker.
2. Reuse `_ensure_review_trigger_comment` with the adopted PR head SHA so the
   same marker/idempotency semantics and review-window timestamp apply. Persist
   the intent before the write, re-read afterward, transition atomically to
   `awaiting_bot_review`, and start one detached worker.
3. Adapt worker/resume guards so independent cycles use their already-bound PR
   and never enter `publishing_initial`. Head changes, closure, auth/rate/network
   failure, timeout, or a pre-Cursor dirty worktree must produce the existing
   recoverable/failed checkpoint without a duplicate GitHub write.
4. Maintain Phase 15 thread eligibility rules: only unresolved threads from the
   configured bot, bound PR head, and request window; never process a duplicate or
   unrelated thread.

### 4. Create and preserve the independent Cursor chat only when needed

1. In the all-actionable branch after schema validation, acquire the run/repo
   locks, revalidate current PR/local head and clean correction baseline, create a
   Cursor chat only if `state.cursor.chat_id is None`, and persist it immediately
   to `state.json` and `cursor/chat.json` before `begin_running_cursor`.
2. Store Codex's exact external fix prompt in the existing sensitive artifact
   path; run the shared workflow engine with the new chat. Do not fabricate an
   initial implementation prompt/iteration or rely on `start_run`'s normal
   pre-implementation checks.
3. Update `resume_pr_review_cycle`, interruption planning, locks, and iteration
   validation so a missing chat is allowed only before the first actionable
   independent decision. Once a chat exists, existing strict same-chat recovery
   rules apply unchanged.
4. After local review succeeds, reuse existing publication code to make a
   Codex-authored commit, non-force push it, verify the exact PR head, resolve only
   the accepted fixed threads, and automatically request the next round. Never
   publish an adopted PR before a genuinely staged, locally reviewed correction.

### 5. Add controlled Cursor model selection

1. Add `pr-review set-cursor-model <run-id> --cursor-model <model>` with no
   implicit fallback. Resolve the run under lock and permit only independent
   cycles in `prepared_independent` or `awaiting_bot_review` with no chat ID,
   no cursor iteration, no local/publication work in progress, no abort request,
   and no live worker that could race the change.
2. Use the existing Cursor compatibility probe/model catalog and the project's
   command before state mutation. On incompatibility/unknown/failed probe, retain
   the prior model and return a redacted error. Do not call an updater unless an
   already-approved explicit tool-update policy is supplied through the existing
   mechanism.
3. Atomically write a sensitive `cursor/model-selection.json` artifact and update
   `state.cursor.model`; include only safe values/hashes/provenance in normal
   status/events. Retain `effective-config.yaml` as the original configuration
   snapshot and add a documented runtime-override artifact rather than rewriting
   historical configuration evidence.
4. Expose current effective Cursor model and whether it is mutable in
   `pr-review status` without exposing prompts, session IDs, or raw probe output.
   Document examples including changing from `auto` to an available explicit
   Cursor model before `pr-review start`.

### 6. Documentation, packaging, and migration

1. Update CLI reference, configuration/reference, autonomous PR-review guide,
   recovery/status guidance, controller skill, and integration documentation to
   distinguish source-run creation from independent adoption. State accurately
   that independent mode requires a human-supplied plan/prompt and exact Codex
   session, but not a prior Cursor chat.
2. Describe the model boundary precisely: `set-cursor-model` changes only the new
   independent Cursor chat before it exists; it does not change the preserved
   Codex reviewer session/runtime or a chat that has already started.
3. Ensure wheel/sdist include any changed schemas, controller skill, and docs.
   Do not claim real GitHub/Cursor/Codex validation from automated fakes.

## Testing Criteria

Automated tests are required. Use isolated temporary repositories and the existing
fake `agent`, `codex`, and `gh` executables only—never real GitHub, SSH, Cursor, or
Codex.

- **Unit — options/state/schema:** command parsing; required PR/branch/plan/prompt
  inputs; origin-aware Pydantic validation and old Phase 15 compatibility; status
  transitions; manifest/sensitive permissions; model-selection eligibility and
  model probe failures; no raw prompts/session IDs/probe output in normal status.
- **Unit — GitHub binding:** explicit PR lookup only; reject closed, cross-repo,
  wrong base, wrong head branch/SHA, remote/local SHA mismatch, dirty/detached
  worktree, auth failure, marker write failure, duplicate active cycle, and drift
  between prepare/start. Verify all `gh` arguments are arrays and redacted.
- **Integration — single and A/B preparation:** prepare creates no chat, Codex
  turn, GitHub write, commit, or push; B's output routes start to A and rejects B
  controller identity; single-session reports inactive-reviewer handoff; start is
  idempotent and spawns only one worker.
- **Integration — feedback path:** no eligible thread keeps chat null; uncertain or
  not-applicable feedback replies `@rojobad` and leaves every thread unresolved
  with no Cursor invocation; all-actionable feedback creates/persists exactly one
  new chat before the first Cursor execution and reuses it through multiple cycles
  and interruption/resume.
- **Integration — publication/recovery:** adopted PR never uses
  `publishing_initial`; actionable corrections use normal staged local review then
  non-force verified publication; only verified fixed threads resolve; drift,
  timeout, abort, auth/rate/network errors, and crash windows remain resumable or
  safely fail without duplicate trigger/reply/resolve/push.
- **Regression:** run relevant Phase 15, model probe, workflow, state-schema,
  controller-skill, docs, formatting, type-check, and package tests. At minimum
  run focused tests first, then the repository's standard `pytest`, `ruff format
  --check`, `ruff check`, `mypy`, and `mkdocs build --strict` commands when
  available.

## Validation

1. Run the complete automated suite above in a native WSL temporary directory.
2. Inspect `--help`, text/JSON status, and generated docs for truthful separate
   flows: existing Phase 15 source-run creation, independent prepare/start, and
   pre-chat Cursor model selection.
3. Conduct a manual disposable-repository acceptance only after automated tests:
   use a pre-authenticated `gh` test account/SSH key, an existing same-repository
   PR, a known exact Codex session, and no production PR. Verify no real model or
   GitHub write occurs until explicit start, then validate one trigger/poll cycle
   and the model-selection command. This is human acceptance, not a Cursor test.

## Risks Or Recovery Notes

- An arbitrary PR may lack trustworthy design context. Requiring immutable plan
  and prompt snapshots is intentional; it prevents an external comment from being
  evaluated without the Codex context owner’s declared scope.
- A local branch can change after prepare. Start and every mutation boundary must
  re-verify the bound PR head; stop instead of rebasing, pulling, resetting, or
  guessing how to reconcile drift.
- A Cursor model selection is safe only before the chat is created. After the
  first actionable decision, changing it would break the one-chat audit contract;
  user must finish/abort or prepare a new independent cycle.
- Existing Phase 15 source-run cycles must deserialize unchanged. Migration must
  be additive and tested; never reinterpret a historic source run as independent.
- The session bridge/hook still cannot prove Codex Desktop hook trust. Document
  that the user confirms `/hooks` trust; never modify it automatically.

## OpenQuestions

None. This phase intentionally treats “cambiar de modelo” as the Cursor model:
the motivating `auto` fallback is a Cursor usage-limit workflow. Codex review
runtime remains bound to the exact supplied reviewer session so the requested
same-Codex-chat guarantee is not weakened.

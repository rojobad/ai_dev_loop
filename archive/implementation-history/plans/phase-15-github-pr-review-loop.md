# Phase 15 — GitHub PR Review Loop

## Goal

Extend `ai_dev_loop` beyond its existing local Cursor → staging → Codex review
loop so that it can autonomously commit/push accepted staged changes, create or
update a pull request, and run a bounded **post-PR review cycle**:

```text
accepted local review
  -> waits for the user's explicit create-PR command
  -> worker commits and pushes the configured branch, then creates/updates the PR to master
  -> worker requests `@codex review` and polls GitHub for that PR/head
  -> worker waits for the configured Codex-bot review at the bound PR head
  -> same exact Codex reviewer session evaluates every unresolved bot thread
  -> all applicable: same Cursor chat fixes, stages, and receives normal local review
  -> worker commits and pushes the fix, verifies publication, resolves only the fixed
     threads, and requests the next review

any rejected or uncertain bot finding
  -> do not invoke Cursor and do not resolve the thread
  -> reply inline as `@rojobad ...` with Codex's explanation
  -> stop at a durable user-attention checkpoint
```

This removes manual copying between GitHub, Codex, and Cursor while preserving the
current exact-session / exact-Cursor-chat identity guarantees. A final human pass
before merging to `master` remains outside this automation.

## Non-Goals

- Do not force-push, merge, close, retarget, or change the base of a PR. Pull requests
  always target `master`; merging remains an explicit human action unless separately
  approved.
- Do not resolve GitHub threads until the worker has committed, pushed, and verified
  the resulting PR head.
- Do not treat any bot review as automatically correct, scrape Markdown to make
  decisions, or forward an ambiguous finding to Cursor.
- Do not store GitHub tokens in configuration, run state, artifacts, logs, or CLI
  output, and do not reuse the Codex Desktop GitHub connector from Python.
- Do not alter the normal local workflow or make GitHub integration mandatory for a
  target repository.

## Scope

- Add an opt-in GitHub CLI adapter, typed PR-review state/artifacts, the Codex Pull
  Requests surface as an optional status UI, and a detached
  autonomous worker that commits/pushes only verified accepted changes, creates or
  updates a bound PR, requests a Codex review, and polls it locally.
- Add a schema-constrained external-feedback Codex turn. It resumes the same reviewer
  session already stored in run state and returns one decision per GitHub review
  thread plus either a complete Cursor prompt or exact inline replies.
- Add a post-PR successor-run constructor that preserves the original Cursor chat and
  Codex reviewer session while deliberately rebasing the repository baseline at an
  explicitly verified PR head.
- Reuse the existing staging and bounded local Codex-review/fix loop for every
  actionable external-review batch.
- Add controller skill routes, explicit start/stop/status controls, and documentation
  for this autonomous post-PR lifecycle.

## Out of Scope

- An inbound GitHub webhook endpoint, a hosted service, a personal access token
  configuration field, or background cloud service. The local detached worker polls
  GitHub through the authenticated CLI; GitHub does not need to reach the user's PC.
- Automatic creation of a PR without an explicit controller command. The PR source is
  the branch recorded at `prepare`; its base is always `master`.
- Processing comments from arbitrary GitHub users; only configured reviewer logins
  and reviews created after the persisted request marker are eligible.
- Mutating non-review GitHub conversation content, labels, assignees, checks, or PR
  metadata.
- Retroactively modifying historical completed runs. New post-PR cycles use explicit
  successor lineage.

## Required Context

Read before implementation:

- `archive/implementation-history/master-plan.md`
- `plan-14-remote-controller-and-review-fork.md`
- `.cursor/rules/ai-dev-loop-*.mdc`, especially Codex-review, loop/resume,
  orchestrator, state/schema, abort, global-integrations, and governance contracts
- `src/ai_dev_loop/{state.py,workflow_engine.py,resume_planner.py,review_result.py}`
- `src/ai_dev_loop/runners/{codex.py,cursor.py,git.py,staging.py}`
- `src/ai_dev_loop/commands/{prepare.py,launch.py,controller.py,start_preflight.py}`
- `src/ai_dev_loop/integrations/codex/{skill,controller_skill}/SKILL.md`
- current CLI/state-schema/configuration/integration tests and the fake CLI fixtures.

## Cursor Rules And Skills

Follow all `.cursor/rules/ai-dev-loop-*.mdc`. Use
`ai-dev-loop-docs-acceptance-governance` for documentation and acceptance work. The
`create-cursor-plan` and `review-staged-changes` skills are planning/review aids, not
runtime dependencies.

## Architecture Guardrails

- **Identity continuity:** every external-feedback decision uses exactly
  `state.codex.session_id`; every correction uses exactly `state.cursor.chat_id`.
  Never use `--last`, create a new Codex review session, create a new Cursor chat,
  or use a subagent as the final reviewer.
- **Published baseline:** a post-PR successor can start only from an explicit bound
  repository/PR/head SHA, a clean worktree, and a verified local HEAD equal to that
  PR head. It records a new baseline; it never attempts to resume a completed run
  whose original pre-implementation HEAD is obsolete.
- **GitHub/SSH boundary:** run `gh` only as an argv array with `shell=False` and a
  narrowly typed request/response model. Authenticate GitHub API calls through the
  user's existing `gh` session, while Git push/pull continues to use the recorded SSH
  remote and a preloaded `ssh-agent` key. Do not log `gh` output, SSH-agent details,
  or auth data verbatim or pass them into artifacts. Validate both API auth and SSH
  push capability before any write.
- **Bounded autonomous writes:** after the user explicitly starts a cycle, its
  detached worker may commit, non-force push the configured branch, create/update the
  bound PR, post `@codex review`, reply inline, and resolve only verified fixed
  threads. Each operation requires persisted idempotency evidence and must be safe to
  retry. Merge, force push, branch/base changes, and all unconfigured remotes remain
  forbidden.
- **Review provenance:** bind one review request to one PR head SHA and request
  marker. Accept only unresolved threads authored by configured bot logins, attached
  to that head/review window, and not already processed by the same cycle. Never
  select a PR, review, or thread by recency alone.
- **All-or-stop adjudication:** Codex returns structured per-thread decisions:
  `actionable`, `not_applicable`, or `uncertain`. If any decision is not actionable,
  do not invoke Cursor, do not resolve any thread, post its exact `@rojobad` inline
  explanation(s), and transition to a durable user-attention checkpoint.
- **Codex authors the content:** Python only validates and forwards Codex's complete
  Cursor prompt and reply body; it must not synthesize, summarize, or reinterpret a
  finding/rejection. Replies must begin with `@rojobad` and state why the comment
  does not apply or what information is missing.
- **Privacy/audit:** raw GitHub comments, Codex decisions, prompts, and replies live
  only in user-only artifacts outside the target repo. State/log/status output stores
  IDs, hashes, safe counts, lifecycle phase, and paths—not comment bodies or tokens.
- **Commit/push gate:** commit/push only after the normal local Codex review returns
  no actionable findings and the approved test policy is satisfied. The commit is
  created from the staged patch only, with a Codex-authored/validated message; push
  uses the persisted upstream ref, verifies its expected remote head, and never uses
  `--force`. Retain locks, abort checks, staging ownership, model probes, and recovery
  semantics around every network, Cursor, staging, Codex, Git commit, and Git push.

## Implementation Plan

### 1. Resolve the GitHub integration contract before implementation

1. Decide the OpenQuestions below and record the approved choices in this plan.
2. Make GitHub support opt-in and disabled by default. Add a versioned optional
   `github` configuration section with only non-secret policy: `command`, allowed
   Codex-bot reviewer logins, trigger body, polling interval/timeout, and the user
   mention (`rojobad`). Reject credentials/tokens in YAML.
3. Add `ai_dev_loop github doctor` to verify `gh` availability, authenticated account,
   API reachability, and only the minimally required repository permissions. Its
   default output must redact account/token details.
4. Build a typed, unit-testable GitHub client adapter around GraphQL/REST calls. It
   must fetch PR metadata/head, review threads with resolution/author/anchor data,
   create the trigger issue comment, post an inline reply, and resolve a thread. Map
   errors/rate limits/timeouts to safe typed outcomes. Do not make an ad-hoc `gh api`
   call anywhere else in the workflow.

### 2. Define post-PR cycle state and immutable artifacts

1. Introduce a typed optional post-PR lineage/binding model rather than adding a
   transition out of terminal `completed` states. A new successor stores its source
   run ID, inherited exact Cursor chat/Codex session, explicit repository/PR number,
   requested head SHA, request marker, cycle number, and safe lifecycle state.
2. Add explicit states/checkpoints for at least: `awaiting_bot_review`,
   `evaluating_bot_feedback`, `waiting_for_user_attention`,
   `fixing_external_feedback`, and `publishing_external_fix`. Keep the existing local
   workflow status machine authoritative for the Cursor/staging/local-review segment.
3. Persist raw GitHub snapshots, validated external Codex result JSON, reply payloads,
   request/response metadata, and hashes as sensitive artifacts in the run directory.
   Persist only redacted references/counts in `state.json`, events, and status output.
4. Extend the run-state schema with optional models; old state files must remain
   readable. Add strict cross-field validation: unique thread IDs, exact SHA formats,
   no reply/resolve action without an eligible bound thread, and no inherited session
   identity replacement.

### 3. Explicitly create the first PR, then request and monitor review

1. Add a controller-only command such as `ai_dev_loop pr-review create <source-run-id>`.
   It is the sole explicit user gate for the initial PR. It verifies the source run
   ended with no unresolved internal findings, has a Cursor chat and Codex session,
   and has the accepted staged patch from the branch recorded at `prepare`.
2. The command has an idempotent publication phase: Codex generates the commit text;
   the worker commits only the accepted staged patch, pushes the tracked SSH branch
   without force, creates/updates one PR from that branch to `master` with Codex's
   title/description, and verifies GitHub's PR head equals the pushed commit. It
   refuses ambiguity, forks, stale heads, unconfigured remotes, or an active cycle.
3. It then creates one idempotent `@codex review` issue comment, records its
   comment ID/time/head marker atomically, and starts/continues a detached bounded
   worker that polls only this review window.
4. The worker must stop on timeout, PR-head change, closed PR, auth failure, rate
   limiting beyond the approved budget, or abort request. It leaves an actionable
   checkpoint and never silently posts another request.

### 4. Evaluate external threads in the original Codex context

1. Add `github-pr-review-result-v1.json` and a typed model. It contains all eligible
   thread IDs, a decision per thread, a Markdown report, test status, a complete
   English `cursor_fix_prompt` when and only when every thread is actionable, and a
   complete inline `@rojobad` reply for every rejected/uncertain thread.
2. Add a deterministic `run_codex_github_review` invocation. Resume the inherited
   exact Codex session, invoke a configured compatible external-feedback review skill,
   include the original plan/prompt, current PR binding/head, and GitHub feedback
   snapshot directly on stdin, and request schema-constrained JSON.
3. Validate full coverage and one-to-one thread mapping. Invalid or incomplete output
   is a safe failure; never parse Markdown as a fallback.
4. On `not_applicable` or `uncertain`, post the exact validated inline reply to each
   affected GitHub comment, leave all threads unresolved, record reply IDs, and stop
   at `waiting_for_user_attention`. Do not run Cursor, staging, or another bot review.
5. Only when every eligible thread is actionable, persist Codex's exact Cursor prompt
   and create the post-PR correction successor.

### 5. Run the correction and existing local review loop

1. Construct the successor with the post-publish baseline, inherited Cursor chat and
   Codex session, and an initial correction iteration driven by the external Codex
   prompt. Do not reuse the old run's staged-patch precondition.
2. Reuse the existing Cursor execution, staging, normal staged-change Codex review,
   bounded fix iterations, artifact redaction, model probing, locks, and abort logic.
   Give the post-PR correction segment its own explicit review-iteration budget.
3. If the normal local reviewer finds issues, retain the existing automatic bounded
   Cursor correction behavior. If it passes, transition to `publishing_external_fix`:
   Codex generates the commit text and the worker commits/pushes the accepted staged
   patch, verifies the updated PR head, then continues automatically.
4. Preserve recoverability for failures/interruption in either the GitHub wait/evaluate
   segment or the existing local workflow. Never rerun a GitHub write or a Cursor turn
   without its persisted idempotency/checkpoint evidence.

### 6. Verify publication, resolve, and request the next pass automatically

1. After publication from the local review loop, verify a clean worktree, expected
   local and remote PR head advancement, the published commit relationship to the
   cycle's staged patch/recorded baseline, and that the bound PR is still open.
2. Only after that verification, resolve exactly the bot threads accepted and fixed by that
   cycle. Never resolve rejected, uncertain, already-resolved-by-someone-else, or
   mismatched-head threads. Surface drift as a user-action checkpoint.
3. Create the next cycle automatically at the new head and issue one idempotent
   `@codex review` request. Do not loop indefinitely: enforce the configured maximum
   external-review cycles and stop with a clear final status after the limit.

### 7. Controller skill, CLI UX, docs, and migration

1. Extend `ai-dev-loop-controller` with natural-language routes for initial PR
   creation, status, abort, and user-attention reporting. It must use the exact
   controller identity, never act from reviewer B, and restate the autonomous external
   write scope before it begins.
2. Update the handoff/controller docs, configuration reference, security/privacy,
   recovery, status/inspect/logs guidance, and target repository integration guide.
   Clearly document that GitHub credentials remain in `gh`, the worker owns accepted
   commits/pushes but not merging, and a non-applicable/uncertain comment stops the
   cycle.
3. Keep all current commands/backwards-compatible run state operational when GitHub
   is absent or disabled.

## Testing Criteria

Automated tests are required. Use fake `agent`, `codex`, and `gh` executables plus
temporary Git repositories; never call GitHub or real models.

- **Unit — config/client/schema:** opt-in config validation; reject token fields;
  `gh` argv shape/redaction/error mapping; GraphQL paging; PR/head/reviewer/window
  filtering; thread and external-result cross-field validation; old state compatibility.
- **Unit — state/lineage:** exact inherited sessions/chats; explicit PR binding;
  forbidden terminal-state mutation; clean/HEAD baseline checks; idempotency markers;
  safe lifecycle transitions and recovery checkpoints.
- **Unit — Codex adjudication:** exact resumed session, configured external review
  skill, structured result only, all-actionable prompt forwarding, invalid/incomplete
  result failure, rejection/uncertainty reply shape beginning with `@rojobad`.
- **Integration — external cycle:** request marker/polling, bot comments attached to
  wrong head or wrong author ignored, head drift/timeout/abort handling, all-actionable
  feedback runs the inherited Cursor chat and normal staged local-review loop, and no
  Cursor execution when any thread is rejected/uncertain.
- **Integration — publication:** Codex-authored commit text; accepted staged patch
  only; SSH non-force push and expected remote-head verification; resolve only the
  eligible fixed thread IDs after verified new head; duplicate work is idempotent;
  mismatch or partial publish makes no GitHub write; bounded next-request behavior.
- **Security/regression:** no full comment/prompt/reply/token/raw `gh` output in
  state/events/logs/default CLI output; no `shell=True`, `--last`, new reviewer
  session, new Cursor chat, commit/push/reset/clean/stash/unstage; existing local,
  controller, abort, recovery, installer, and documentation tests still pass.

## Validation

Run focused configuration/GitHub-client/state/external-review/publication tests, then:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m build
uv run mkdocs build --strict
```

Manual acceptance is a separate, explicit follow-up in a disposable PR with an
already-authenticated `gh` account. It must verify the real bot trigger, an actionable
thread, and an uncertain/rejected thread; it must not use production credentials in
automated tests.

## Risks Or Recovery Notes

- GitHub reviews are asynchronous and may be edited, resolved, or superseded. Treat
  the persisted PR head/request marker/thread IDs as the source of truth; head drift
  pauses the cycle rather than mixing feedback across commits.
- A `gh` session can lack access despite a successful executable check. Fail before
  writes where possible and preserve a redacted checkpoint if access changes later.
- External replies are public collaboration writes. They must be generated by the
  exact Codex reviewer, persisted before sending, idempotent, and never sent for an
  action that has not been explicitly authorized by the controller command.
- GitHub may not expose a stable reply endpoint/identifier combination through the
  chosen API. Verify the API contract with the fake adapter and a user-approved
  disposable PR before enabling the feature by default.
- An automatic publication can be unsafe if the worktree contains unrelated changes
  or the remote advanced. Verify the accepted staged-patch identity and expected
  remote head; reject ambiguity rather than committing, force-pushing, or claiming the
  correction was published.

## OpenQuestions

1. **Resolved — GitHub credential boundary:** Require a pre-authenticated GitHub CLI
   (`gh`) in WSL, with no token stored by `ai_dev_loop`; keep Git remotes on SSH and
   require the user's SSH key to be preloaded in `ssh-agent`. Install/configure `gh`
   separately for Codex Desktop's Windows Pull Requests view when that view is used.
2. **Resolved — autonomous publication authority:** After an explicit create-PR
   command, the worker may commit the accepted staged patch, push the recorded branch
   without force, create/update the PR, resolve verified fixed threads, and post the
   next `@codex review`. Merge, close, retarget, force push, and base changes remain
   excluded.
3. **Resolved — PR creation/base policy:** Create a PR only on an explicit controller
   command. Its head is the branch recorded at `prepare`; its base is `master`.
4. **Resolved — commit, test, and PR text policy:** Codex generates each commit
   subject/body and each PR title/description. A failing/blocked test does not block
   the next cycle when the local Codex reviewer determines it is unrelated to the
   change and returns no corrective action; it must record/comment the residual risk.
   The worker does not automatically merge to `master`.
5. **Resolved — review source:** The initial and only allowed bot login is
   `chatgpt-codex-connector`; retain the value as repository configuration for a
   future deliberate change.
6. **Resolved — rejection/uncertainty resumption:** The worker pauses after its
   `@rojobad` reply and accepts only the GitHub command
   `@rojobad /ai-dev-loop continue`; ordinary prose is never authorization.
7. **Resolved — external-cycle budget:** Configure the maximum in the target
   repository YAML and support at least `8` external cycles.
8. **Resolved — polling behavior:** Poll GitHub locally every 60 seconds, for up to
   24 hours. Do not add an inbound webhook/relay.
9. **Resolved — interruption recovery:** An explicit controller command resumes a
   persisted interrupted cycle; do not install a user-level startup service.

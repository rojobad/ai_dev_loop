# Prepare a Target Repository for `ai_dev_loop`

## Objective

Integrate a target project repository with the external `ai_dev_loop` orchestrator.

This plan prepares the repository so an approved Codex plan can be handed to `ai_dev_loop`, while keeping orchestration state, logs, session metadata, Cursor output, Codex review artifacts, and loop recovery data outside the target repository.

The target repository integration is intentionally small. The global `ai-dev-loop-handoff` skill, installed by `ai_dev_loop`, is responsible for running `ai_dev_loop prepare`. The target repository does not need to implement the orchestration workflow itself.

## Background and Rationale

Before `ai_dev_loop`, the workflow was manual:

1. Codex discussed the change with the user and wrote an implementation plan.
2. The user copied a Cursor prompt into a Cursor chat.
3. Cursor implemented the change.
4. The user staged the changes.
5. The user prompted Codex to review staged changes with the project review skill and pasted Cursor's final response for context.
6. If Codex found actionable findings, the user copied only those findings into Cursor with a high-signal instruction header such as: "Verify whether these issues exist, then fix the confirmed issues only."
7. The user repeated the review/fix cycle manually until Codex found no actionable issues.

`ai_dev_loop` automates that loop while preserving the same agent roles:

- Codex remains the architect, context owner, and reviewer.
- Cursor remains the implementation and correction agent.
- `ai_dev_loop` is only the local orchestrator that transports exact prompts, stages changes, resumes the original Codex session, records artifacts, enforces limits, and fails safely.

The target repository changes in this plan exist to give that orchestrator a stable contract:

- `ai_dev_loop.yaml` tells the orchestrator which local commands, models, skill names, timeouts, and conventions to use.
- The separate prompt file gives Cursor a durable implementation prompt that does not depend on conversation memory.
- The `.gitignore` rule keeps temporary prompt files out of source control while still allowing `ai_dev_loop` to snapshot and hash them.
- Review skill compatibility ensures Codex can return both the normal human Markdown review and the machine-readable JSON decision the loop needs.
- The `cursor_fix_prompt` guidance preserves the quality improvement from the previous manual workflow: Cursor receives a focused verification-and-fix prompt containing only actionable findings, not the whole review report.

This plan deliberately does not move orchestration logic into the target repository. Earlier versions of the integration expected each repository's planning skill to parse configuration and call `ai_dev_loop prepare` directly. The current preferred design is lighter: the global `ai-dev-loop-handoff` skill performs the handoff, and the target repository only supplies repository-specific configuration, plan/prompt files, and review-skill compatibility.

An AI agent applying this plan should therefore avoid building another orchestrator inside the target repository. The goal is to prepare the repository as an input to `ai_dev_loop`, not to duplicate `ai_dev_loop`.

## What This Repository Must Provide

The target repository must provide:

- a root `ai_dev_loop.yaml`;
- an ignored prompt-file convention under `docs/plans/`;
- an implementation plan file for each approved change;
- a separate Cursor prompt file for each approved plan;
- a staged-change review skill compatible with `ai_dev_loop` structured review output.

The target repository must not provide:

- XDG state paths;
- `state.json`;
- run IDs;
- Cursor chat IDs;
- Codex session mappings;
- loop logs;
- agent output artifacts;
- hook installation logic;
- global skill installation logic.

## External Prerequisites

Before using a target repository with `ai_dev_loop`, install and verify the global integration from the `ai_dev_loop` repository.

For Codex CLI inside WSL:

```bash
ai_dev_loop integrations install --target wsl-cli
```

For Codex Desktop on Windows with agents running inside WSL:

```bash
ai_dev_loop integrations install \
  --target codex-desktop-wsl \
  --wsl-distro <distro-name> \
  --install-session-bridge
```

Then trust the `ai_dev_loop` hook from `/hooks` in the Codex surface that owns the interactive planning session. For Codex Desktop, trust the hook in Codex Desktop, not in a separate WSL Codex CLI.

Verify when needed:

```bash
ai_dev_loop integrations status --target codex-desktop-wsl --wsl-distro <distro-name>
ai_dev_loop integrations sessions status
```

Do not set `CODEX_HOME` in WSL to the Windows Codex Desktop home. Do not symlink the whole Codex home. The supported Desktop/WSL bridge is the nested sessions bridge managed by `ai_dev_loop`.

## Scope

### Included

- Add `ai_dev_loop.yaml` at the target repository root.
- Add `docs/plans/prompt_*.txt` to `.gitignore`.
- Ensure planning produces both:
  - `docs/plans/<plan-name>.md`;
  - `docs/plans/prompt_<plan-name>.txt`.
- Verify the configured review skill name exists.
- Make the review skill compatible with schema-constrained Codex output if needed.
- Add lightweight repository documentation only if it helps maintainers.
- Validate the configuration with `ai_dev_loop config validate`.

### Optional

- Update an existing project-specific planning skill so it always writes the separate prompt file.
- Add a short note to the planning skill telling users to invoke `$ai-dev-loop-handoff` after plan approval.
- Add a repository-local checklist for plan/prompt preparation.

### Excluded

- Do not install global Codex hooks from the target repository.
- Do not install global Codex skills from the target repository.
- Do not run Cursor directly from the project planning skill.
- Do not run `codex exec resume` directly from the project planning skill.
- Do not call `ai_dev_loop start` from the active interactive Codex session.
- Do not store `ai_dev_loop` run artifacts in the target repository.
- Do not commit, tag, push, reset, clean, stash, or unstage as part of this integration plan.

## Repository Configuration

Create this file at the repository root:

```text
ai_dev_loop.yaml
```

Use this schema:

```yaml
version: 1

project:
  name: my-project

cursor:
  command: agent
  model: composer-2.5-fast
  output_format: stream-json
  force: true
  trust_workspace: true
  sandbox: disabled

codex:
  command: codex
  review_model: gpt-5.5
  review_skill: review-staged-cursor-execution
  sandbox: workspace-write

workflow:
  max_review_iterations: 3
  require_clean_worktree: true
  stage_mode: all
  cursor_timeout_minutes: 90
  codex_timeout_minutes: 90

prompt:
  directory: docs/plans
  filename_template: prompt_{plan_stem}.txt

# Enable only when the autonomous post-PR cycle is required.
github:
  enabled: true
  command: gh
  reviewer_logins:
    - chatgpt-codex-connector
  review_trigger_body: "@codex review"
  poll_interval_seconds: 60
  poll_timeout_hours: 24
  max_external_cycles: 8
  user_mention: rojobad
  continue_command: "@rojobad /ai-dev-loop continue"
  external_review_skill: review-github-pr-feedback
  max_local_review_iterations: 3
  pr_base: master
```

Replace:

- `project.name` with a stable lowercase slug for the repository.
- `cursor.model` with the exact Cursor CLI model available in the local WSL `agent` installation.
- `codex.review_model` with a model available to the Codex account that will run `codex exec resume`.
- `codex.review_skill` with the exact frontmatter name of the repository's staged-change review skill.
- timeouts only when the project genuinely needs different limits.

Do not add project architecture rules to `ai_dev_loop.yaml`. Architecture instructions belong in plans, repository rules, and skills.

Validate:

```bash
ai_dev_loop config validate --repo /path/to/target-repo
```

## Prompt File Convention

For each plan:

```text
docs/plans/<plan-stem>.md
```

create a matching prompt file:

```text
docs/plans/prompt_<plan-stem>.txt
```

Example:

```text
docs/plans/phase-4-active-organization-request-context.md
docs/plans/prompt_phase-4-active-organization-request-context.txt
```

The prompt file must contain the exact prompt intended for Cursor's first implementation turn.

Rules:

- Keep the prompt separate from the Markdown plan.
- Re-read both files before handoff.
- If the plan changes materially, update the prompt file before handoff.
- Do not rely on Codex conversation memory as the source of truth for the prompt.
- Do not delete the prompt file automatically after `prepare`; it may be useful for inspection or a new run.

Add this to `.gitignore`:

```gitignore
docs/plans/prompt_*.txt
```

If the repository already has a local-artifacts section, put the rule there.

## Planning Skill Guidance

If the target repository already has a project-specific planning skill, keep its existing planning quality and repository-specific guardrails.

Required behavior:

- It should create or update the Markdown plan.
- It should create or update the matching prompt file.
- It should present both paths to the user.
- It should wait for explicit plan approval before suggesting handoff.
- It should not run Cursor directly after this integration.
- It should not run `ai_dev_loop start`.

Recommended final guidance after the plan is approved:

```text
Plan approved.

Plan:
docs/plans/<plan-stem>.md

Cursor prompt:
docs/plans/prompt_<plan-stem>.txt

To prepare the automated loop, invoke:
$ai-dev-loop-handoff
```

The planning skill may call `ai_dev_loop prepare` directly only if the repository deliberately wants that tighter coupling. The preferred default is to let the global `ai-dev-loop-handoff` skill perform prepare, because it already knows how to use the SessionStart context and the current `ai_dev_loop` CLI contract.

## Handoff Workflow

After the plan and prompt are approved, ask Codex:

```text
Use $ai-dev-loop-handoff to prepare the run.

Repo: /path/to/target-repo
Plan: docs/plans/<plan-stem>.md
Prompt: docs/plans/prompt_<plan-stem>.txt

Do not run ai_dev_loop start. Only run prepare and return the start_command.
```

The global skill should:

- confirm the plan is final;
- confirm the prompt file exists;
- read `ai_dev_loop.yaml`;
- use the exact current Codex session ID from SessionStart context;
- run `ai_dev_loop prepare`;
- pass the prompt file's exact contents through stdin;
- return the generated `start_command`.

Then leave the active Codex session idle or exit it, and run from WSL:

```bash
ai_dev_loop start <run-id>
```

Do not use `--last`. The original session ID must come from the trusted SessionStart context or a manually verified Desktop session ID.

## Review Skill Compatibility

The configured review skill remains the project-specific authority for review methodology.

The skill may continue to define a human Markdown review format, but it must be compatible with `ai_dev_loop` structured review output:

- The final Codex answer for automated review must be valid JSON matching `codex-review-result-v1.json`.
- The human Markdown report belongs inside the `review_markdown` field.
- `has_actionable_findings`, `findings_count`, `highest_severity`, `tests_status`, and `cursor_fix_prompt` drive the loop decision.
- The orchestrator must not scrape Markdown to extract findings.

If the existing review skill says "respond only in Markdown" or otherwise forbids structured output, update it with an `ai_dev_loop` compatibility note.

### Cursor Fix Prompt Quality

When actionable findings exist, the review skill must make Codex produce `cursor_fix_prompt` as a complete prompt for the same Cursor chat.

This prompt should intentionally mirror the high-signal manual workflow:

```text
Verify whether the issues below exist in the current repository state, then fix the confirmed issues only.

Do not make unrelated changes.
Preserve the approved plan and the existing staged work.
After fixing, stop and report what changed.

Findings:

<only actionable findings here>
```

Rules for `cursor_fix_prompt`:

- Include a clear instruction header before the findings.
- Include only actionable findings, not the entire review report.
- Do not include general summary text, test-status prose, residual-risk notes, or non-actionable observations unless they are required to understand a finding.
- Preserve severity labels, file paths, line references, expected behavior, actual behavior, and reasoning when available.
- Tell Cursor to verify each issue exists before changing code.
- Tell Cursor to fix only confirmed issues and avoid unrelated changes.
- Do not ask Cursor to commit, push, reset, clean, stash, or alter unrelated staged work.
- Do not invent new findings while creating the fix prompt.

The orchestrator will store and forward `cursor_fix_prompt` exactly as returned by Codex. Therefore, the review skill is responsible for producing the header and narrowing the content to actionable findings.

Suggested compatibility note:

```markdown
## ai_dev_loop Compatibility

When this skill is invoked by `ai_dev_loop`, keep the normal review methodology and Markdown report format, but place the Markdown report in the `review_markdown` field of the required structured JSON response. Do not require a Markdown-only final answer in that automated context.

If actionable findings exist, provide a complete `cursor_fix_prompt` in English. The prompt must start with a clear instruction header telling Cursor to verify whether the listed issues exist, fix only confirmed issues, avoid unrelated changes, preserve the approved plan and existing staged work, and then report what changed. Include only actionable findings in that prompt, not the full review report, test-status prose, residual-risk notes, or non-actionable observations. If no actionable findings exist, set `cursor_fix_prompt` to null.
```

Do not otherwise rewrite the review methodology unless an actual incompatibility is found.

## Optional GitHub PR Review Cycle

Enable this only for the bounded autonomous post-PR cycle. `gh` must already be
authenticated and Git push must use SSH with a loaded `ssh-agent` key; never put
tokens, passphrases, or credentials in `ai_dev_loop.yaml`.

The target repository must provide an external-feedback skill whose frontmatter
name matches `github.external_review_skill` (normally
`review-github-pr-feedback`). It is distinct from the staged-change review
skill, is read-only, and returns the exact `github-pr-review-result-v1.json`
contract: one decision per supplied bot thread, all-or-stop behavior,
`@rojobad` replies for non-applicable/uncertain comments, and a Cursor prompt
only when every finding is actionable. Keep it under `.agents/skills/` or
otherwise discoverable by the exact Codex session. The skill must never post,
resolve, commit, push, or merge; `ai_dev_loop` owns those writes.

There are two explicit entry paths:

1. `ai_dev_loop pr-review create <source-run-id>` publishes accepted staged
   changes from a completed normal run, creates/updates the PR to `master`, then
   requests `@codex review`.
2. `pr-review prepare` plus `pr-review start` adopts an existing PR that did not
   pass through the main loop. It requires an explicit PR number, checked-out
   branch, local `HEAD` equal to the PR head, plan, original Cursor prompt, and
   exact Codex reviewer session. `prepare` creates no chat and performs no
   GitHub write; `start` is the separate explicit write gate.

```bash
ai_dev_loop pr-review prepare \
  --repo-path /path/to/repo --pr <number> --branch <branch> \
  --plan-path docs/plans/<plan>.md \
  --prompt-source-path docs/plans/prompt_<plan>.txt \
  --codex-session-id <reviewer-session> \
  [--controller-session-id <controller-A>] \
  < docs/plans/prompt_<plan>.txt

ai_dev_loop pr-review start <run-id> \
  [--controller-session-id <controller-A>]
```

In A/B, B prepares with its reviewer session and remains inactive; A starts
with its controller session. In single-session mode the reviewer must be
inactive before `start`. Never use `--last` or infer a session.

For independent PR adoption, pass `--cursor-model <model>` at prepare or, while
the cycle has no Cursor chat, run:

```bash
ai_dev_loop pr-review set-cursor-model <run-id> --cursor-model <model>
```

This changes only the new Cursor chat's model; it never changes the exact Codex
reviewer session/runtime. Once the chat exists, it is frozen for that run.

Before a live start, run:

```bash
ai_dev_loop github doctor --repo-path /path/to/repo
```

The worker binds one PR/head/request window, accepts only matching unresolved
bot threads, and stops with inline `@rojobad` replies and unresolved threads if
any decision is non-applicable or uncertain. After all-actionable feedback it
performs the normal local review, non-force verified publication, resolves only
verified fixed threads, and requests the next round. Merge remains manual.

## Validation Plan

Run target-repository-appropriate checks:

```bash
ai_dev_loop config validate --repo /path/to/target-repo
git check-ignore docs/plans/prompt_example.txt
```

Also verify manually or with lightweight tests:

- `ai_dev_loop.yaml` parses successfully.
- The project slug is correct.
- Cursor model identifier is valid for local `agent`.
- Codex review model is valid for local `codex`.
- `codex.review_skill` matches an installed or repository-discoverable skill frontmatter name.
- `docs/plans/prompt_*.txt` is ignored by Git.
- The planning workflow creates a plan and matching prompt file.
- The prompt file is re-read from disk before handoff.
- The review skill does not forbid JSON output in the automated `ai_dev_loop` context.
- When findings exist, the review skill can produce a `cursor_fix_prompt` with a verification/fix header and only actionable findings.
- `cursor_fix_prompt` does not include the whole Markdown review report, test-status section, or unrelated summary text.
- No XDG state path or run artifact path was added to the repository.

Optional dry prepare, after global integration is installed and a real session ID is available:

```bash
ai_dev_loop prepare \
  --repo-path /path/to/target-repo \
  --plan-path docs/plans/<plan-stem>.md \
  --prompt-source-path docs/plans/prompt_<plan-stem>.txt \
  --codex-session-id "<exact-session-id>" \
  --output json < /path/to/target-repo/docs/plans/prompt_<plan-stem>.txt
```

Do not run `ai_dev_loop start` during repository integration unless the user explicitly wants to execute the automated loop.

## Acceptance Criteria

- The repository contains a valid root `ai_dev_loop.yaml`.
- The repository ignores `docs/plans/prompt_*.txt`.
- The plan/prompt convention is documented or encoded in the planning workflow.
- The configured review skill exists.
- The review skill is compatible with schema-constrained `ai_dev_loop` output.
- If GitHub PR review is enabled, the configured external-feedback skill exists,
  returns the GitHub-thread JSON contract, and performs no GitHub writes itself.
- If GitHub PR review is enabled, `ai_dev_loop github doctor --repo-path` passes
  with authenticated `gh` and SSH push readiness.
- Both source-run `pr-review create` and independent `prepare`/`start` are
  documented accurately; neither path uses `--last` or creates an implicit PR.
- The review skill instructs Codex to generate high-signal `cursor_fix_prompt` content for Cursor: header plus actionable findings only.
- The repository does not contain `ai_dev_loop` run state, logs, session mappings, Cursor artifacts, or Codex artifacts.
- The repository does not install global hooks or global skills.
- The default handoff path uses `$ai-dev-loop-handoff`.
- The active Codex session is not used concurrently with `ai_dev_loop start`.

## Rollback

To roll back repository integration:

- remove `ai_dev_loop.yaml`;
- remove the `.gitignore` rule if it is no longer wanted;
- revert any planning skill note or prompt-file convention changes;
- revert any review skill compatibility note only if the repository will not use `ai_dev_loop`.

Do not delete global `ai_dev_loop` integrations from a target repository rollback. Global integrations are managed from the `ai_dev_loop` installation:

```bash
ai_dev_loop integrations uninstall --target <target>
```

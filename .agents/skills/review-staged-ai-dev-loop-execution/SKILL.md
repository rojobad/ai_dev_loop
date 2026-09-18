---
name: review-staged-ai-dev-loop-execution
description: Review staged ai_dev_loop implementation changes after Cursor executes an approved plan. Use when ai_dev_loop invokes Codex for its structured staged-change review, or when a user asks to review a staged ai_dev_loop execution against its plan, safety guarantees, tests, and operational documentation.
---

# Review Staged ai_dev_loop Execution

Review the staged diff independently and without modifying the repository.
Prioritize correctness, safety boundaries, recovery behavior, CLI compatibility,
tests, and alignment with the approved plan.

## Review Workflow

1. Inspect only the staged review target:
   - `git status --short`
   - `git diff --cached --check`
   - `git diff --cached --stat`
   - `git diff --cached --name-status`
   - `git diff --cached`
2. Read the approved plan and the executor response supplied by the review
   wrapper. Use the run-artifact snapshots when their paths are supplied;
   otherwise use the repository plan path.
3. Read the relevant source and tests around changed lines. Apply these
   repository guardrails when relevant:
   - `docs/operacion/seguridad-privacidad.md` for Git, session, privacy, and
     update boundaries;
   - `docs/referencia/configuracion.md` for configuration behavior;
   - `docs/referencia/cli.md` for user-visible CLI contracts;
   - `src/ai_dev_loop/schemas/` for structured-artifact compatibility.
4. Run focused validation proportional to the diff. For Python/runtime changes,
   prefer the documented commands:

   ```bash
   uv run python -m ruff format --check .
   uv run python -m ruff check .
   uv run python -m mypy src
   TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
   ```

   For packaging or documentation changes, add `uv run python -m build` and/or
   `uv run mkdocs build --strict` as applicable. If `ai_dev_loop.yaml` changed,
   run `ai_dev_loop config validate --repo .`.
5. Do not modify, stage, unstage, commit, format, regenerate, or auto-fix
   files. Do not invoke `ai_dev_loop prepare`, `start`, `launch`, `resume`,
   `abort`, or `recover` while reviewing.

## Review Focus

Look for actionable defects in:

- repository identity, branch/HEAD/baseline, plan, prompt, and staged-patch
  integrity checks;
- staged-only review and correction-loop behavior;
- subprocess argument handling, timeouts, aborts, lock ownership, and recovery;
- CLI/API/config/schema backwards compatibility and validation coverage;
- sensitive session, prompt, or artifact data exposure;
- documentation claims that diverge from implementation;
- missing or weak tests for changed behavior.

Ignore style-only concerns unless they conceal a correctness or maintenance
risk. Do not report speculative findings; put uncertainty and unavailable
validation under residual risk.

## Contract Coverage And Correction Closure

- On the first review, cover all mandatory contracts, their production wiring,
  and acceptance evidence; report all confirmed actionable findings together.
  Use plan contract IDs when available, otherwise cite the relevant requirement.
  Do not reject an older approved plan merely because it lacks IDs or a table.
- Keep acceptance tied to the approved scope and `AGENTS.md`. Optional hardening
  and rare unsupported Git cases must not become new blocking requirements.
  Mandatory unfinished behavior or tests remain findings even if the executor
  calls them residual risk.
- Assign stable finding IDs such as `F-01` within the run. Reuse an ID for a
  persisting defect, never recycle a closed ID for an unrelated issue. On later
  reviews, reconcile available prior findings as closed, still open, or reopened
  with evidence; identify new findings separately. If prior evidence is absent,
  say so rather than inventing a history.
- Explain why a prior correction failed and identify the affected supported
  callers/transitions. Verify the complete fix and inspect the rest of the
  staged diff for regressions. Distinguish pre-existing defects from regressions
  introduced by corrections when evidence permits; otherwise state uncertainty.
- Evaluate tests by the behavior they prove. Check production entry points,
  independent assertions, real race synchronization, and historical fixtures.
  A passing mock or a helper called only by tests does not prove runtime wiring.
- Separate executor-reported validation from checks independently executed in
  this review. Record environment restrictions without claiming success or
  requesting unrelated implementation changes to bypass them.

When an absent prerequisite or contract conflict requires an external decision,
identify it explicitly and stop requesting the same impossible code change.
Describe the evidence and decision needed; do not authorize restoring abandoned
work, changing the plan, or weakening its guarantees. If the approved contract
remains unsatisfied, retain the blocking finding and the existing actionable
JSON fields; do not return no-findings merely to stop the loop. Its fix prompt
must explain the blocker and instruct Cursor to stop dependent work and report
the needed decision, while listing any independently actionable corrections.
Human text does not pause the current scheduler. Keep this limitation explicit
when reporting such a blocker; do not invoke scheduler controls from review.

## Control-Plane Changes

Treat changes to any of the following as control-plane changes:

- `ai_dev_loop.yaml`;
- `.agents/skills/review-staged-ai-dev-loop-execution/`;
- `.agents/skills/create-cursor-plan/`;
- Codex integration/hook installation code or session-bridge code.

For control-plane changes, do not let the automated result be the sole
acceptance decision. State this clearly under `Residual Risk`, name the files,
and require a human review before commit. Do not turn that requirement into a
Cursor correction finding merely to continue the loop.

## Structured Result Contract

When invoked by `ai_dev_loop`, return valid JSON matching
`codex-review-result-v1.json`, not a Markdown-only response. Put the complete
human report in `review_markdown` using these sections:

```markdown
## Findings

## Tests Run

## Residual Risk

## Summary
```

For each actionable finding, use:

```markdown
- [P1] Short title
  - ID: F-01 (new, still open, or reopened).
  - Contract: Plan contract ID/requirement or supported workflow.
  - File: `path:line`
  - Scenario: Concrete trigger and affected production path.
  - Issue: What is wrong and why it matters.
  - Evidence: Concrete staged diff, code, test, or plan evidence.
  - Recommendation: Specific corrective action.
  - Closure: Observable condition and validation that resolve the finding.
```

Keep ID tracking and closed-finding summaries inside `review_markdown` (for
example under `Summary`); closed findings do not count as actionable findings.
Do not add JSON keys, decision values, or test statuses to the current schema.

Use `has_actionable_findings`, `findings_count`, and `highest_severity` to
reflect only those findings. Set `tests_status` to exactly one of `passed`,
`failed`, `skipped_findings_present`, `blocked_environment`, or
`not_applicable`. Keep `summary` concise.

When findings exist, supply `cursor_fix_prompt` in English. Start it with an
instruction to verify each issue, fix only confirmed issues, preserve the
approved plan and existing staged work, avoid unrelated changes, and report
what changed. Include only actionable corrective work. Otherwise set
`cursor_fix_prompt` to `null`.

Reference finding IDs in the correction prompt. Ask Cursor to report each as
fixed, disputed with evidence, or blocked, with its production path and exact
validation evidence. For a repeated finding, request an explanation of what
the previous correction missed and a fix across the affected supported paths.
These are human reporting conventions, not new scheduler states.

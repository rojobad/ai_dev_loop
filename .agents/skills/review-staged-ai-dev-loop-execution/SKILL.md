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
  - File: `path:line`
  - Issue: What is wrong and why it matters.
  - Evidence: Concrete staged diff, code, test, or plan evidence.
  - Recommendation: Specific corrective action.
```

Use `has_actionable_findings`, `findings_count`, and `highest_severity` to
reflect only those findings. Set `tests_status` to exactly one of `passed`,
`failed`, `skipped_findings_present`, `blocked_environment`, or
`not_applicable`. Keep `summary` concise.

When findings exist, supply `cursor_fix_prompt` in English. Start it with an
instruction to verify each issue, fix only confirmed issues, preserve the
approved plan and existing staged work, avoid unrelated changes, and report
what changed. Include only actionable corrective work. Otherwise set
`cursor_fix_prompt` to `null`.

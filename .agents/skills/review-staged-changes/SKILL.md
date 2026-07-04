---
name: review-staged-changes
description: Review staged Git changes in a strict code-review format. Use when the user asks to review staged/stagged changes, staged diff, cambios staged/stagged, changes ready to commit, or asks for a code review of what is currently staged. Focus on performance, architecture compliance, correctness, tests, safety, and alignment with the requested change.
---

# Review Staged Changes

## Workflow

Perform a code-review pass over the staged Git changes only.

1. Inspect repository state:
   - `git status --short`
   - `git diff --cached --stat`
   - `git diff --cached --name-status`
   - `git diff --cached --check`
2. Read the staged diff with `git diff --cached`.
3. Open relevant files around changed lines when the diff alone is not enough.
4. Review for:
   - correctness and behavioral regressions;
   - performance and scalability;
   - architecture compliance and local patterns;
   - security, privacy, and operational safety;
   - test quality and missing coverage;
   - alignment with the user-requested change and stated plan.
5. Do not modify files, stage, unstage, commit, or run destructive commands unless the user explicitly asks for fixes.

## Severity

- `P0`: Blocks release or can cause data loss, security compromise, or widespread outage.
- `P1`: High-impact correctness, safety, or contract issue that should be fixed before merge.
- `P2`: Meaningful bug, maintainability problem, architecture drift, or important test gap.
- `P3`: Minor issue, polish, small robustness improvement, or low-risk cleanup.

## Output

Respond in English by default. Put findings first. Use exactly this shape for each finding:

```text
- [P<severity>] <imperative or descriptive title>
  - File: `<path>:<line>`
  - Issue: <what is wrong and why it matters>
  - Evidence: <specific diff/code/test/config evidence>
  - Recommendation: <concrete fix or validation to add>
```

Keep file references tight. Prefer the line where the defect is introduced or where the reviewer should start reading. If a finding spans multiple files, use the primary file in `File:` and mention related files in `Evidence:`.

If there are no actionable findings, say so clearly:

```text
No actionable findings.

Residual risk: <tests not run, areas not inspectable, or "none identified">
```

If useful, add a short `Notes` section after findings for non-blocking observations or validation that passed. Do not include a long summary before findings.

## Review Discipline

- Report only actionable issues grounded in the staged changes.
- Do not list speculative problems without evidence.
- Prefer fewer, higher-signal findings over broad commentary.
- Mention missing tests only when the staged change creates meaningful risk.
- If the user provided a plan, prompt, ticket, or stated goal, check whether the staged changes satisfy it and flag mismatches.
- If generated artifacts, bytecode, secrets, build outputs, or unrelated files are staged, report them.

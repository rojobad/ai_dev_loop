---
name: ai-dev-loop-docs-acceptance-governance
description: Codex-side governance for ai_dev_loop documentation authoring and acceptance review. Use when Codex writes Phase 8.5 MkDocs documentation, reviews Phase 8 acceptance findings, updates documentation from code/finding evidence, or checks docs against ai_dev_loop architecture, safety, privacy, integration, and cleanup guardrails.
---

# ai_dev_loop Codex Docs And Acceptance Governance

Use this skill when Codex is authoring, reviewing, or planning documentation and acceptance handoffs for `ai_dev_loop`.

Cursor implementation work is governed by `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`. This skill is the Codex-side companion for documentation authoring and review.

## Source Of Truth

- `archive/implementation-history/master-plan.md` is the archived master contract.
- Archived phase plans under `archive/implementation-history/plans/` preserve scope history.
- Phase findings files under `archive/implementation-history/findings/` are audit artifacts and handoff context.
- `.cursor/rules/*.mdc` are mandatory guardrails for Cursor-driven work.
- Do not invent behavior in docs. Verify against code, schemas, CLI help, tests, plans, and findings.

## Phase 8 Boundary For Codex Review

- Phase 8 owns final acceptance hardening, package validation, real workstation validation, and MkDocs scaffolding.
- Phase 8 must not write the final documentation set; Codex should treat Phase 8 docs content as scaffolding and handoff material only.
- Configure MkDocs with Material for MkDocs.
- Keep `/docs/index.md` intentionally minimal.
- Move `scripts/codex-desktop-wsl-sessions.md` to `docs/reference/codex-desktop-wsl-sessions.md` as a Phase 8.5 placeholder.
- Do not restore `scripts/share-codex-desktop-sessions.sh`.
- Do not implement destructive state cleanup.

## Phase 8.5 Boundary

- Phase 8.5 owns the real documentation content and is expected to be Codex-authored.
- Final docs must use the MkDocs site, not loose reference files.
- Docs must be grounded in code, CLI help, schemas, tests, plans, and findings. Do not invent behavior.
- Docs should cover architecture, installation, integration targets, Codex Desktop/WSL bridge, hook trust, target repo config, handoff workflow, prepare/start/resume/abort, status/logs/inspect/list, state layout, safety model, troubleshooting, privacy, cleanup, and uninstall.
- Cleanup remains a manual policy unless a later explicit plan approves a destructive command.

## Real Workstation And Model Calls

- Real model calls are allowed only when explicitly approved by the user or the active phase plan.
- Automated tests must use fake `agent` and fake `codex` executables.
- If WSL distro detection is ambiguous during real desktop integration checks, use `Ubuntu-22.04`.
- Treat Codex Desktop `/hooks` trust as already completed only because the user explicitly said so; if the hook definition changes, report that renewed trust may be required.
- If no exact desktop session ID is available for real model-backed smoke validation, stop. Do not guess, shorten, infer, search heuristically, or use `--last`.
- Do not run real acceptance smoke tests against the `ai_dev_loop` source repository.

## Non-Negotiable Guardrails

- Do not use `--last`.
- Do not share the entire Windows `.codex` home with WSL.
- Do not set WSL `CODEX_HOME` to `/mnt/c/.../.codex`.
- Do not symlink, copy, migrate, open, or inspect Codex SQLite databases across Windows and WSL.
- Keep the desktop bridge limited to rollout session files.
- Do not bypass or mutate Codex hook trust state.
- Do not commit, push, tag, reset, clean, stash, unstage, or perform destructive cleanup.
- Do not print full prompts, fix prompts, staged patches, raw JSONL, auth payloads, full environments, transcript contents, or full session IDs in default output.

## Findings Handoff

Every future final acceptance or documentation phase should create or update an archived findings file, for example under `archive/implementation-history/findings/`, with:

- commands run;
- validation results;
- real workstation actions;
- real model smoke outcomes, if any;
- blocked manual steps;
- residual risks;
- exact follow-up scope for the next phase.

## Documentation Review Checklist

When reviewing or writing final docs, verify:

- docs match implemented CLI flags and command names;
- docs identify when real model calls or real workstation installation are required;
- docs never recommend `--last`;
- docs never recommend sharing Windows and WSL `.codex` homes;
- docs explain hook trust as manual;
- docs explain cleanup as manual policy, not an implemented destructive command;
- docs do not expose prompts, transcript contents, raw JSONL, auth payloads, or full session IDs.

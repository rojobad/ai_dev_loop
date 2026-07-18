---
name: create-cursor-plan
description: Create an execution plan and concise handoff prompt for Cursor. Use when the user asks Codex to make, draft, update, or generate a plan/prompt for Cursor or another coding agent to implement repository work, especially phased implementation work that must preserve architecture guardrails, Cursor rules/skills, scope boundaries, non-goals, testing criteria, automated test requirements, and unresolved OpenQuestions.
---

# Create Cursor Plan

## Workflow

1. Read the user's request and inspect the repository enough to understand existing planning conventions.
2. Read any referenced plan, findings, architecture notes, rules, skills, tickets, or docs before writing the handoff.
3. Identify relevant Cursor rules and skills already present in the repo, such as `.cursor/rules/*`, `.cursor/rules/*.mdc`, `.cursor/skills/*`, `AGENTS.md`, or project-specific agent docs.
4. Create or update the plan artifact in the repo's existing plan location. If no convention exists, use `plans/` unless the user specified another path.
5. Create or update a separate concise prompt artifact when the user asks for a prompt or when the plan is meant to be handed to Cursor.
6. Do not execute the plan unless the user explicitly asks.

## Required Plan Sections

Every Cursor execution plan must include these sections, using clearer project-specific names when useful:

- Goal or Goals
- Non-Goals
- Scope
- Out of Scope
- Required Context
- Cursor Rules And Skills
- Architecture Guardrails
- Implementation Plan
- Testing Criteria
- Validation
- Risks Or Recovery Notes
- OpenQuestions

Use `OpenQuestions` exactly as the section name. If there are no unresolved questions, write `None.`.

## OpenQuestions Rule

Do not make assumptions for decisions that affect architecture, data ownership, safety, external integrations, irreversible operations, security, privacy, migrations, or public behavior.

When clarification is needed:

- Put the question in `OpenQuestions`.
- Mark any dependent implementation step as blocked until the question is answered.
- Make the Cursor prompt tell Cursor to stop and resolve OpenQuestions before making dependent changes.
- Do not hide the uncertainty in wording like "probably", "maybe", or "assume".

Only make low-risk mechanical assumptions when they follow a repo convention already present in files, and name the convention briefly.

## Cursor Rules And Skills

The plan must list all relevant repo-local Cursor governance inputs that Cursor should follow, including:

- `.cursor/rules/*`
- `.cursor/skills/*`
- `AGENTS.md`
- project-specific review, planning, or coding rules

If no Cursor rules or skills exist, state that explicitly and recommend creating one when the work has reusable governance needs.

When a rule or skill is required for the plan, mention it both in the plan and in the concise prompt.

## Architecture Guardrails

Always include architecture guardrails as concrete constraints, not vague preferences. Focus on boundaries such as:

- source of truth
- adapter/infrastructure boundaries
- domain versus transport concerns
- persistence and caching responsibilities
- security and secret handling
- test and validation boundaries
- forbidden adjacent work
- migration or rollout safety

Write guardrails so Cursor can check whether an implementation step violates them.

## Scope Discipline

Separate `Non-Goals` from `Out of Scope`:

- `Non-Goals` explain outcomes this phase intentionally does not try to achieve.
- `Out of Scope` lists concrete files, systems, workflows, or behaviors Cursor must not modify.

Call out phase boundaries explicitly when the request is part of a multi-phase plan.

## Testing Requirements

Always include `Testing Criteria` as a separate plan section. It must state what evidence should prove the implementation is correct.

Require automated tests when the planned work changes behavior, public APIs, CLI behavior, configuration loading, persistence, data access, subprocess execution, parsing, validation, error handling, security checks, or recovery logic.

When automated tests are required, the plan must specify:

- test level: unit, integration, end-to-end, contract, or regression
- expected test files or fixture areas when discoverable
- relevant edge cases and failure paths
- fake or stub strategy for external CLIs, network calls, model calls, credentials, and timeouts
- commands Cursor should run when tooling is available

When automated tests are not appropriate, the plan must say why and provide manual validation steps. Do not leave test creation implicit.

## Prompt Shape

The Cursor prompt must be much shorter than the plan. It should:

- point to the plan path
- tell Cursor to follow listed rules and skills
- preserve architecture guardrails
- tell Cursor not to assume and to stop on OpenQuestions before dependent work
- tell Cursor to follow the testing criteria and add/update automated tests when the plan requires them
- name the highest-risk boundaries in one short paragraph
- include validation command expectations only when they are essential

Do not duplicate the plan in the prompt.

Preferred prompt template:

```text
Please implement the plan in <plan-path>. Follow the Cursor rules and skills listed in the plan, keep the architecture guardrails intact, follow the testing criteria, add/update automated tests where required, avoid assumptions, and stop to resolve any OpenQuestions before making dependent changes.

Pay special attention to <2-5 highest-risk guardrails or boundaries>.
```

Add project-specific command instructions only when important, for example:

```text
Use Docker Compose for project commands.
```

## File Naming

Follow repo conventions first. For this repository, archived implementation plans
belong under `archive/implementation-history/plans/`. If no other convention
exists:

- Plan: `archive/implementation-history/plans/phase-N-short-slug.md`
- Prompt: `archive/implementation-history/plans/prompt_phase-N-short-slug.txt`

Use lowercase slugs with hyphens for plan names and the same slug after `prompt_` for prompt names.

## ai_dev_loop Handoff

After the user explicitly approves both artifacts, tell them to invoke the
global `$ai-dev-loop-handoff` skill with the plan and prompt paths. Do not run
Cursor, `ai_dev_loop prepare`, `start`, `launch`, or `resume` from this
planning skill.

The target configuration is `ai_dev_loop.yaml`; keep the plan and prompt under
the configured archive path. Treat changes to `ai_dev_loop.yaml`, this
planning skill, or the staged-review skill as control-plane work: call them
out in the plan and require a manual acceptance review rather than presenting
an automated loop result as sufficient acceptance.

## Final Response

Report the plan path and prompt path. Include the prompt text in the final response when the user asked for something they can pass to Cursor.

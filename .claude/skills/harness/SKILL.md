---
name: harness
description: Generate or refactor a project-specific Claude Code agent team from a repo and a brief. Use when the user wants custom agent personas, CLAUDE.md orchestration, or reusable team scaffolding created for a specific codebase instead of hand-authoring every file.
when_to_use: Trigger on requests like "make me an agent team for this repo", "generate Claude agents for this project", "create project-specific subagents", "build a CLAUDE.md workflow for this codebase", or "turn this into a reusable agent-team repo".
argument-hint: <team-brief>
effort: high
---

# Harness

Create or update a project-specific Claude Code agent-team kit for the current repository.

Treat the user's brief as the desired end state:

$ARGUMENTS

## What to produce

Create only the files the repo actually needs:

1. `.claude/agents/*.md` agent personas
2. `.claude/CLAUDE.md` or root `CLAUDE.md` orchestration instructions, depending on the repo's conventions
3. `.claude/skills/*` only when a repeated procedure deserves a skill instead of bloating `CLAUDE.md`
4. `README.md` updates when the repo is meant to be shared or installed elsewhere

## Workflow

1. Inspect first. Read the repo's `README.md`, `CLAUDE.md`, manifests, test config, and any existing `.claude/agents` or `.claude/skills`.
2. Extract the real constraints: stack, risk areas, delivery lifecycle, testing expectations, deployment surface, and recurring chores.
3. Design the smallest team that can cover the work. Prefer 3 to 6 sharp roles over many blurry ones.
4. Reuse existing roles only when they truly fit. Replace generic roles with domain roles when the domain changes the review bar.
5. Keep the main Claude session as orchestrator. Never make a teammate responsible for spawning or managing the team.
6. Write or update the files.
7. Verify the kit is internally consistent, then summarize how to use it.

## Agent design rules

- Give every agent one clear lane, explicit deliverables, and stop conditions.
- Make descriptions easy for Claude to route against later.
- Encode security, testing, and review expectations at the right level for the repo.
- Prefer concrete repo-specific commands and paths over generic advice.
- Do not invent tools, frameworks, or processes that the repo does not use unless the user asked for them.
- Preserve user content. Append or patch carefully instead of replacing broad files wholesale.

For role-shaping heuristics and common failure modes, read [references/role-shaping.md](references/role-shaping.md).

## Skill decision rule

Create an additional skill only when the repo has a reusable procedure that is:

- repeated,
- multi-step,
- easy to forget, and
- awkward to keep inline in `CLAUDE.md`.

Otherwise keep the workflow in `CLAUDE.md` or the agent prompts.

## For shared team repos

If the repo is itself a reusable agent-team kit, read [references/shareable-team-repo.md](references/shareable-team-repo.md) before editing `README.md` or install instructions.

## Final check

Before finishing, confirm:

- agent roles are non-overlapping
- orchestration lives in `CLAUDE.md`, not inside a teammate
- setup instructions match the actual files in the repo
- new skills have accurate frontmatter and install paths
- the final summary includes a recommended first prompt

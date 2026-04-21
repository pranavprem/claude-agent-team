# Shareable Team Repo Contract

Use this when the repo itself is a reusable Claude Code team kit that another Claude instance should be able to install.

## README goals

The root `README.md` should let Claude set up the kit with minimal interpretation. Keep the instructions deterministic and ordered.

A good README for a shareable team repo usually includes:

1. a one-line description of what the kit contains
2. explicit setup steps for Claude
3. overwrite-safety rules
4. any required settings merge
5. verification steps
6. quick-start usage examples
7. a file structure section that matches the actual repo

## Install-step expectations

If the repo ships agent personas, tell Claude exactly where to copy them, usually `~/.claude/agents/`.

If the repo ships skills, copy whole skill directories into `~/.claude/skills/`, not just individual `SKILL.md` files.

If the repo ships a reusable `CLAUDE.md`, prefer append-or-merge instructions over replace instructions unless the user explicitly asked for replacement.

If settings are required, give Claude the exact JSON keys to merge and specify which values to overwrite versus preserve.

## Safety rules to state explicitly

- ask before overwriting an existing agent file or skill directory
- preserve unrelated user configuration
- append to `CLAUDE.md` instead of replacing it unless told otherwise
- report verification results back to the user

## Quick-start expectations

Show at least two kinds of usage:

- how to run the installed team on a real task
- how to invoke any included skill that materially changes the workflow

Use concrete prompts, not generic placeholders alone.

## File-structure expectations

The file tree in `README.md` should reflect the repo as it exists after your edits. If you add a skill, show the skill directory and any important reference files.

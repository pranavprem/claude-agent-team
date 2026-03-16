# Claude Code Agent Team 🤖🏗️

A battle-tested agent team setup for Claude Code that turns a single AI session into a full engineering team — architect, reviewers, developer, tester, and a retrospective analyst that makes the team smarter after every session.

## What's Inside

**7 specialized agent personas** that work together through Claude Code's [agent teams](https://code.claude.com/docs/en/agent-teams) feature:

| Agent | Role | Personality |
|-------|------|-------------|
| **architect** | System Designer | 18yr Principal Architect (AWS/Netflix). Produces detailed design docs. |
| **architecture-reviewer** | Design + Conformance | 22yr Distinguished Engineer. Reviews designs AND validates implementations match. |
| **developer** | Implementer | 15yr Staff SWE. Clean, linted, well-commented code. Language-agnostic. |
| **tester** | Test Engineer | 16yr Principal SDET. Unit, integration, e2e, and security tests. |
| **code-reviewer** | Code Reviewer | 20yr Staff Engineer. Read-only. Security → correctness → performance → quality. |
| **retrospective** | Session Analyst | Extracts learnings and updates agent prompts, CLAUDE.md, and memory files. |

The **main Claude session acts as team lead** (manager), orchestrating the pipeline:

```
architect → arch-reviewer ⟲ → developer → tester → code-reviewer ⟲ → arch conformance → commit → retrospective
```

## Prerequisites

- [Claude Code](https://code.claude.com) v2.1.32+ (`claude --version`)
- [tmux](https://github.com/tmux/tmux/wiki) for split-pane display (recommended)
- A terminal emulator (Ghostty, iTerm2, etc.)

## Installation

```bash
git clone https://github.com/pranavprem/claude-agent-team.git
cd claude-agent-team
./install.sh
```

The installer:
- Copies agent personas to `~/.claude/agents/`
- Installs `CLAUDE.md` with orchestration instructions to `~/.claude/`
- Enables agent teams in `settings.json` with tmux mode
- Backs up any existing files before overwriting

**Dry run** (see what would change without modifying anything):
```bash
./install.sh --dry-run
```

## Usage

### Full Pipeline (complex features)

```bash
# 1. Start tmux
tmux new -s dev

# 2. Navigate to your project
cd ~/your-project

# 3. Launch Claude Code
claude

# 4. Tell it to create a team
```

Prompt:
```
Create an agent team to build [your feature].
Follow the development lifecycle in CLAUDE.md.
```

Claude creates the team, spawns agents in separate tmux panes, and orchestrates the full 8-phase pipeline automatically.

### Simplified Variants

**Architecture + Implementation** (medium complexity):
```
Create an agent team. Task: [your task]
Spawn architect and developer. Architect designs, developer implements.
```

**Implementation + Review** (well-understood tasks):
```
Create an agent team. Task: [your task]
Spawn developer, tester, and code-reviewer.
Developer implements, tester writes tests, code-reviewer reviews.
```

**Individual agents** (quick tasks):
```
Use the code-reviewer subagent to review my recent changes
Use the architect to design the new auth module
```

### tmux Navigation

With `teammateMode: "tmux"`, each agent gets its own pane:

| Action | Command |
|--------|---------|
| Switch panes | Click (with mouse on) or `Ctrl+B` + arrow key |
| Jump to pane by number | `Ctrl+B` then `q` then number |
| Zoom/unzoom a pane | `Ctrl+B` then `z` |
| Show task list | `Ctrl+T` |

**Tip:** Enable mouse support in tmux:
```bash
echo "set -g mouse on" >> ~/.tmux.conf
tmux source-file ~/.tmux.conf
```

## The 8-Phase Pipeline

1. **Architecture** — Architect produces a design document (components, data flow, API contracts, security, error handling)
2. **Architecture Review** ⟲ — Reviewer evaluates the design. Loop until approved.
3. **Implementation** — Developer implements following the approved architecture
4. **Testing** — Tester writes comprehensive tests (happy path, edge cases, errors, security)
5. **Code Review** ⟲ — Reviewer checks code. Loop with developer until approved.
6. **Architecture Conformance** — Reviewer validates implementation matches the design
7. **Completion** — Atomic commits, push
8. **Retrospective** — Analyst extracts learnings, updates agent prompts & CLAUDE.md

## Design Principles Baked In

Every agent has these principles embedded:

- **Security first** — validate input, parameterize queries, least privilege, never log secrets
- **Clean code** — self-documenting names, small functions, no dead code, DRY (rule of three)
- **Meaningful comments** — explain *why*, not *what*
- **Typed errors** — specific, actionable error messages
- **Immutability by default** — const/final/readonly everywhere
- **Test behavior, not implementation** — tests survive refactoring
- **Lint and format always** — follow the project's existing style

## Customization

### Modify agent personas
Edit files in `~/.claude/agents/`. Each agent is a markdown file with YAML frontmatter:

```yaml
---
name: architect
description: "System Architect. Designs scalable, secure architectures..."
tools: Read, Grep, Glob, Bash, Write, Edit, WebFetch, WebSearch
model: opus
---

You are a **Principal Software Architect**...
```

### Change the model
Edit the `model:` field in each agent's frontmatter. Options: `opus`, `sonnet`, `haiku`, or `inherit` (use session default).

**Cost optimization:** Use `opus` for architect + code-reviewer (need strongest reasoning), `sonnet` for developer + tester (good coding, cheaper).

### Update the pipeline
Edit `~/.claude/CLAUDE.md` to change the phase order, add phases, or modify management principles.

### Add project-specific context
Create `.claude/CLAUDE.md` in your project root for project-specific conventions, gotchas, and architecture notes. Agents read both user-level and project-level CLAUDE.md.

## How It Gets Smarter

The **retrospective** agent runs at the end of every session and:

1. Analyzes what went right and wrong
2. Classifies learnings as universal (all projects) or project-specific
3. Updates agent prompts with new checklist items
4. Updates CLAUDE.md with new standards
5. Writes to memory files for future reference

Over time, your team evolves — common mistakes get caught earlier, patterns get codified, and each agent's instructions become more refined.

## File Structure

```
claude-agent-team/
├── README.md                    # This file
├── install.sh                   # Installer script
├── CLAUDE.md                    # User-level orchestration instructions
├── settings.json                # Agent teams enabled + tmux mode
├── architect.md                 # System architecture agent
├── architecture-reviewer.md     # Design review + conformance agent
├── developer.md                 # Implementation agent
├── tester.md                    # Test engineering agent
├── code-reviewer.md             # Code review agent
└── retrospective.md             # Session learning agent
```

## Credits

Built by [pranavprem](https://github.com/pranavprem) with [Neo](https://github.com/openclaw/openclaw) 🌀

Inspired by:
- [Claude Code Agent Teams docs](https://code.claude.com/docs/en/agent-teams)
- [Claude Code Sub-agents docs](https://code.claude.com/docs/en/sub-agents)
- The community patterns from [awesome-claude-agents](https://github.com/rahulvrane/awesome-claude-agents)

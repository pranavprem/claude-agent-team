# Role Shaping

Use these heuristics when generating a project-specific team.

## Start from outcomes, not titles

Pick roles based on the work the repo repeatedly needs:

- architecture and decomposition
- implementation
- testing and validation
- security or compliance review
- domain-specific review, if the domain changes the failure modes
- retrospection and prompt improvement

Do not keep a role just because the default team had it.

## Prefer a small sharp team

Most repos need 3 to 6 agents. Add more only when the responsibilities are genuinely distinct.

Good splits:
- **architect** vs **developer**
- **tester** vs **code-reviewer**
- **security-reviewer** vs **application-reviewer** when security deserves its own pass
- **data-engineer** or **infra-reviewer** when the repo's risk is operational rather than product logic

Bad splits:
- two agents both responsible for "quality"
- an agent that only rephrases another agent's work
- a manager/orchestrator subagent, because the main Claude session must own orchestration

## Replace generic roles when the domain matters

Swap in domain roles when they raise the quality bar. Examples:

- Home Assistant repo: integration-architect, HA-integration-reviewer, device-test-engineer
- Trading system: strategy-reviewer, execution-risk-reviewer, data-quality-tester
- Infra repo: platform-architect, terraform-reviewer, release-verifier
- Prompt or agent repo: prompt-architect, eval-designer, safety-reviewer

Keep the pipeline recognizable, but adapt the titles and review rubrics to the real work.

## What every agent prompt should include

Each agent file should define:

1. the role and seniority
2. the kind of output expected
3. non-negotiables specific to that lane
4. what to inspect before acting
5. when to stop and hand back control

Agent prompts should be opinionated, but not bloated.

## Common failure modes

Avoid these:

- overlapping agents that produce redundant feedback
- vague descriptions that Claude cannot route against later
- prompts that mention tools or commands the repo does not have
- pushing orchestration into a teammate prompt
- copying a generic lifecycle into a repo that clearly needs a different sequence
- stuffing repeatable procedures into `CLAUDE.md` when they should be skills

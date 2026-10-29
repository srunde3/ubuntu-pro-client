# Design context for pro-e2e

This folder records why pro-e2e is shaped the way it is: the domain it models, the decisions behind it, the designs in progress and the questions still open. The code, [README.md](../README.md) and the skills say what the tool does. This folder says why, so an agent can check a change against the intent before making it.

## Read in this order

1. [domain.md](domain.md): the concepts and their lifecycles. They hold whatever test framework runs the scenarios.
2. [decisions.md](decisions.md): what is settled, and why.
3. [designs/](designs/): designs under review, or accepted and not yet built. A built design is folded into the files above and deleted.
4. [open-questions.md](open-questions.md): what is not decided yet.

## Other sources answer other questions

| Question | Source |
| --- | --- |
| What tools, commands, settings and defaults does the tool have? | [README.md](../README.md) |
| Which layer does a change go in? What is on disk? How do the processes work? | the [pro-e2e-architecture](../.claude/skills/pro-e2e-architecture/SKILL.md) skill |
| How is each layer tested? | the `pro-e2e-*-tests` skills, listed in [AGENTS.md](../AGENTS.md) |
| What did the user say in review? | the review docs below, which live outside the repository |

The review docs:

- Architecture review, with the "Goal architecture" and "Components and vocabulary" tabs: <https://claude.ai/code/artifact/a732dbf9-6694-4752-943d-1d4f42760dd8>. It is a Claude Docs document, readable only with the Claude Docs connector.
- Framework seam design (built; its decisions are DEC-22 to DEC-40): <https://claude.ai/artifact/VXfoHQDNFo9AjK8Vpx6xmF>.

## Keeping this folder current

- When the user settles a question, move it from [open-questions.md](open-questions.md) into [decisions.md](decisions.md) or into its design, in the same change as the code or doc it affects. Keep its ID.
- Each design has a status: in review, accepted or built. Once a design is built, fold what lasts into [domain.md](domain.md) and [decisions.md](decisions.md), and delete the design file.
- Copy what matters from a review doc into this folder. The review docs live outside the repository and need tools an agent may not have.
- Describe the design as it is or will be. History belongs in commit messages.

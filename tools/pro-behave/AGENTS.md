# AGENTS.md for tools/pro-behave

This is a standalone `uv` project, `pro-behave`: it runs ubuntu-pro-client's behave scenarios as jobs and records campaigns of them, from the `pro-behave` command or over MCP. Its one package, `pro_behave`, has four parts: `jobs/`, `campaign/`, `mcp/` and `cli.py`. It is a local project with no consumers. Nothing here runs in CI.

## Read before changing code

| Skill | Read it when |
| --- | --- |
| [pro-behave-architecture](.claude/skills/pro-behave-architecture/SKILL.md) | before any change. It covers the layers, where a change goes, the design and change rules, the test layout, and the checks every change must pass. |
| [pro-behave-unit-tests](.claude/skills/pro-behave-unit-tests/SKILL.md) | writing or reviewing tests for one layer |
| [pro-behave-integration-tests](.claude/skills/pro-behave-integration-tests/SKILL.md) | testing an MCP tool, a CLI command, server wiring, startup, or the real feature files |
| [pro-behave-e2e-tests](.claude/skills/pro-behave-e2e-tests/SKILL.md) | a change that needs real behave jobs to prove it |

[README.md](README.md) documents the tool surface, env vars and defaults for people running the server.

[agent-docs/](agent-docs/README.md) records the domain, the settled decisions and their reasons, designs in progress and open questions. Read it before proposing a change to the design, and update it in the same change when a decision is made.

## Where the root AGENTS.md does not apply

The root [AGENTS.md](../../AGENTS.md) applies here, terminology and safety rules included, with these exceptions:

- The package targets Python 3.10 or later. The root's Python 3.5 requirement does not apply.
- Dependencies are declared in this `pyproject.toml` and locked in `uv.lock`. Run everything with `uv`, not `tox`.
- Tests MUST use hand-written fakes and `monkeypatch`, not `mock`/`unittest.mock`. A mock is acceptable only when the test cares that a call happened and nothing about what the call does.
- Tests MAY be plain functions. Grouping them in classes is optional.
- The checks in the architecture skill replace `tox -e test` and the root's lint envs. All of them MUST pass before a change is done.

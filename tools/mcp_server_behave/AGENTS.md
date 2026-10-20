# AGENTS.md for tools/mcp_server_behave

This is a standalone `uv` project with two packages: `behave_mcp` runs behave jobs, serves the feature catalog and hosts the MCP server, and `behave_campaign` keeps the campaign record, schedules its jobs and provides the `behave-campaign` CLI. It is a local project with no consumers. Nothing here runs in CI.

## Read before changing code

| Skill | Read it when |
| --- | --- |
| [behave-mcp-architecture](.claude/skills/behave-mcp-architecture/SKILL.md) | before any change. It covers the layers, where a change goes, the design and change rules, the test layout, and the checks every change must pass. |
| [behave-mcp-unit-tests](.claude/skills/behave-mcp-unit-tests/SKILL.md) | writing or reviewing tests for one layer |
| [behave-mcp-integration-tests](.claude/skills/behave-mcp-integration-tests/SKILL.md) | testing an MCP tool, a CLI command, server wiring, startup, or the real feature files |
| [behave-mcp-e2e-tests](.claude/skills/behave-mcp-e2e-tests/SKILL.md) | a change that needs real behave jobs to prove it |

[README.md](README.md) documents the tool surface, env vars and defaults for people running the server.

## Where the root AGENTS.md does not apply

The root [AGENTS.md](../../AGENTS.md) applies here, terminology and safety rules included, with these exceptions:

- The package targets Python 3.10 or later. The root's Python 3.5 requirement does not apply.
- Dependencies are declared in this `pyproject.toml` and locked in `uv.lock`. Run everything with `uv`, not `tox`.
- Tests MUST use hand-written fakes and `monkeypatch`, not `mock`/`unittest.mock`. A mock is acceptable only when the test cares that a call happened and nothing about what the call does.
- Tests MAY be plain functions. Grouping them in classes is optional.
- The checks in the architecture skill replace `tox -e test` and the root's lint envs. All of them MUST pass before a change is done.

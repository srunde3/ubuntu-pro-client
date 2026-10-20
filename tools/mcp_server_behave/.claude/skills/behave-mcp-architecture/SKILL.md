---
name: behave-mcp-architecture
description: Architecture and design rules for tools/mcp_server_behave, the behave MCP server and its behave_campaign package. Use before changing code in tools/mcp_server_behave, when adding or changing an MCP tool, a CLI command or a config variable, and when deciding which module a change belongs in.
---

# behave MCP architecture

`tools/mcp_server_behave` is a standalone `uv` project for Python 3.10 and later. The root repository's Python 3.5 rule does not apply here. It ships two packages:

- `behave_mcp` runs single behave jobs, serves the feature catalog, and hosts the MCP server.
- `behave_campaign` keeps the durable record of a campaign, schedules its jobs, and provides the `behave-campaign` CLI.

## Dependency direction

`behave_campaign` may import `behave_mcp.parser`, `behave_mcp.pools` and `behave_mcp.layout`. Nothing in `behave_mcp` imports `behave_campaign`, except the tool wrappers in `behave_mcp/server.py`. Keep it that way, so the module graph stays acyclic and `behave_campaign` can be extracted.

`uv run lint-imports` enforces these rules and the layer table below, using the contracts under `[tool.importlinter]` in `pyproject.toml`. If a change breaks a contract, first move the code to the layer it belongs in. Change a contract only when the architecture itself changes, in the same commit, with the reason in the commit message.

## Layers

Both packages use ports and adapters, with one module per layer.

| Layer | `behave_mcp` | `behave_campaign` | Rule |
| --- | --- | --- | --- |
| Domain | `domain.py`, `parser.py`, `pools.py`, `layout.py` | `domain.py`, `discovery.py`, `export.py` | Pure rules. No file, process or network I/O, except reading feature files through `parser.py`. |
| Ports | `ports.py` | `ports.py` | `Protocol` interfaces the services depend on. |
| Adapters | `adapters.py` | `adapters.py`, `repo.py` | Implement the ports with the filesystem, `subprocess`, `fcntl` and git. |
| Services | `service.py` (`BehaveService`) | `service.py` (`CampaignService`), `runner.py` (`CampaignRunner`) | Orchestrate domain and ports. Most behaviour changes belong here. |
| DTOs | `messages.py` | `messages.py`, `views.py` | Pydantic models returned to front-ends; `views.py` builds them from records. |
| Front-ends | `server.py` | `cli.py` | Thin wrappers: parse arguments, call one service method, serialise the result. |
| Config | `config.py` | none | Env vars parsed once into `Settings`. |

Small collaborators are passed as plain callables, not ports: the clock (`now_utc`, `now`, `monotonic`), `sleep`, `new_job_id` and the git read (`repo_state`). Tests replace them with lambdas.

## Where a change goes

| Change | Put it in |
| --- | --- |
| A rule about units, attempts, lifecycle, scheduling, classification or log parsing | the package's `domain.py` |
| A new kind of external interaction | a `Protocol` in `ports.py`, an adapter in `adapters.py`, and a fake for tests |
| What a tool or command does | the service method |
| A new MCP tool | a wrapper in `server.py` registered with the local `@tool`, plus the README and the agent skills |
| A new CLI command | a wrapper in `cli.py` over a `CampaignService` method |
| A new env var | `config.py` (`Settings` and its parser) and the README's environment section |
| A new path under the state directory | `layout.py`, which names paths and reads no env vars. The caller resolves `MCP_STATE_DIR` and passes it in. |

Register MCP tools with the local `@tool` decorator in `server.py`, never with `@mcp.tool`. It returns one compact JSON text block and publishes no output schema, which keeps results and the tool list small.

## State on disk

Everything the server writes lives under `<repo_root>/.mcp_server_behave/`, or under `$MCP_STATE_DIR` when it is set:

| Path | Holds | Written by |
| --- | --- | --- |
| `jobs/<job_id>_stdout.log` | combined stdout and stderr of the job | the behave subprocess |
| `jobs/<job_id>_report.json` | behave's JSON report | the behave subprocess |
| `jobs/<job_id>_meta.json` | the job record (`JobRecord`) | `BehaveService` |
| `jobs/index.jsonl` | one `started` and one `completed` row per job | `BehaveService` |
| `campaigns/<id>.jsonl` | the campaign record: header, plan, started, finished, lifecycle and retry records, append only | `CampaignService`, `CampaignRunner`, CLI |
| `campaigns/<id>.events.jsonl` | numbered events | `JsonlEventLog` |
| `campaigns/<id>.lock` | the run lock (`flock`) while a campaign schedules | `FileCampaignRunLock` |
| `exports/<id>.tar.gz` | the evidence bundle | CLI `export` |

A campaign's state is never stored. It is rebuilt by replaying its record file through `domain.reduce_units` and `domain.lifecycle`.

## Process model

- One long-lived server process, serving MCP over streamable HTTP at `http://MCP_HOST:MCP_PORT/mcp`. FastMCP runs tool calls on worker threads.
- `server.py` holds the long-lived objects: one `BehaveService`, one `InMemoryJobRegistry`, one `CampaignRunner` and one `JsonlEventLog`. `CampaignService` is built per call.
- One daemon ticker thread calls `CampaignRunner.tick` every 2 s for the one active campaign. The runner's guard serialises ticks and control verbs.
- Each behave job is a `tox -e behave` subprocess in its own session, with stdin from `/dev/null`. `kill_job` signals the whole process group.

## Surface rules

- Shape the service and CLI first: what a response carries, and how many calls a task takes. Tune MCP defaults, limits and schema size separately. Token cost justifies only the tuning.
- Reuse the existing taxonomy. A combo is `{release, machine_type}`, and a unit is a scenario on one combo. Do not invent notation such as `release/machine_type` strings.
- Every list that grows takes a `limit`, and reports `total` and `truncated` (plus `limit_clamped` when the limit was capped).
- Reject unknown filter values with an error. An empty result would hide the typo.
- Raise `BehaveServiceError` or `CampaignError` for caller mistakes. FastMCP reports them as `isError`, and the CLI prints them and exits 2. Return an expected outcome that is not an error, such as `capacity_exceeded` or `timeout`, as its own typed response.

## Duplication and vocabulary

Duplication across `behave_mcp`, `behave_campaign` and the parser is intentional, so each part can move upstream on its own. Do not merge copies or add cross-package imports or sync tests. Copy small reference lists from `features/` by hand, as `pools.py` does for machine types.

Copies must agree on the concept. A field, enum value or term that means the same thing has the same name and the same values in every copy. When you touch a copy that disagrees, make it agree.

## Change policy

- The package has no consumers. Make a breaking change when it improves the design, for example its maintainability, its surface or its duplication, and say which in the commit. Add no compatibility shims, optional fields or legacy readers. Mark the change with `!` in the commit type, and tell the user.
- Commit messages are concise and use conventional commits: `fix(jobs): ...`, `feat(campaign)!: ...`.
- Code should explain itself. Keep comments short. If behaviour needs a long comment to be understood, raise it with the user.
- Code comments and docstrings describe the current behaviour only, never history.
- `README.md` is the source of truth for the tool surface, env vars and defaults. Update it in the same commit as the change.
- When a tool's name, parameters or behaviour changes, update `.github/skills/feature-test-runs/` and `.github/skills/run-sru-test-suite/` in the same commit.

## Tests

The test tree mirrors the packages, so a failing file says which part broke:

| Directory | Holds |
| --- | --- |
| `tests/behave_mcp/` | tests of `behave_mcp`'s layers, one file per module, plus `test_golden.py` for on-disk byte shapes |
| `tests/behave_campaign/` | the same for `behave_campaign`, plus `test_cli.py` |
| `tests/shared/` | tests of `parser.py` and `pools.py`, which both packages depend on |
| `tests/mcp_surface/` | the tools over the real MCP protocol, and the two e2e tests |

Each directory is a Python package, so test files with the same name can coexist. Every fake lives in `tests/conftest.py`, one per port, so tests across tiers share it. The `behave-mcp-unit-tests`, `behave-mcp-integration-tests` and `behave-mcp-e2e-tests` skills describe what each tier tests.

## Checks

Run these from `tools/mcp_server_behave`. Every one must pass before a change is done:

```bash
uv sync --extra test --extra lint
uv run pytest -q -m "not e2e"
uv run black --check behave_mcp behave_campaign tests
uv run isort --check-only behave_mcp behave_campaign tests
uv run flake8 behave_mcp behave_campaign tests
uv run mypy behave_mcp behave_campaign
uv run lint-imports
```

`uv sync --extra lint` alone removes pytest from the environment. Always sync both extras together. This is a local project, so nothing runs these checks for you.

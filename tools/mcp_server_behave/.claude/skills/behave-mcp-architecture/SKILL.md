---
name: behave-mcp-architecture
description: Architecture and design rules for tools/mcp_server_behave, the behave MCP server and its behave_campaign package. Use before changing code in tools/mcp_server_behave, when adding or changing an MCP tool, a CLI command or a config variable, and when deciding which module a change belongs in.
---

# behave MCP architecture

`tools/mcp_server_behave` is a standalone `uv` project for Python 3.10 and later. The root repository's Python 3.5 rule does not apply here. It ships two packages:

- `behave_mcp` runs single behave jobs, serves the feature catalog, and hosts the MCP server.
- `behave_campaign` keeps the durable record of a campaign, schedules its jobs, and provides the `behave-campaign` CLI.

## Dependency direction

`behave_campaign` may import `behave_mcp.parser`, `behave_mcp.domain.pools` and `behave_mcp.domain.layout`, and its `cli.py` may also import `behave_mcp.service` and `behave_mcp.adapters`, because every front-end configures the checkout and reconciles its jobs. Nothing in `behave_mcp` imports `behave_campaign`, except the tool handlers in `behave_mcp/server.py`. Keep it that way, so the module graph stays acyclic and `behave_campaign` can be extracted.

`uv run lint-imports` enforces these rules and the layer table below, using the contracts under `[tool.importlinter]` in `pyproject.toml`. If a change breaks a contract, first move the code to the layer it belongs in. Change a contract only when the architecture itself changes, in the same commit, with the reason in the commit message.

## Layers

Both packages use ports and adapters. A layer with more than one concern is a package with one module per concern.

| Layer | `behave_mcp` | `behave_campaign` | Rule |
| --- | --- | --- | --- |
| Domain | `domain/`: `jobs`, `queue`, `config`, `report`, `logs`, `limits`, `pools`, `layout` | `domain/`: `records`, `state`, `schedule`, `classify`, `events`, `signals`, `discovery`, `bundle` | Pure rules. No file, process or network I/O, except `discovery` reading feature files through the parser. |
| Parser | `parser.py` | none | Feature files to structured data. It becomes its own package later. |
| Views | `views.py` | `views/`: `responses`, `export` | Pure projections of domain and parser types into DTOs and export files. A view decides nothing. |
| Ports | `ports.py` | `ports.py` | `Protocol` interfaces the services depend on. |
| Adapters | `adapters.py` | `adapters.py`, `repo.py` | Implement the ports with the filesystem, `subprocess`, `fcntl` and git. |
| Services | `service.py` (`BehaveService`) | `service.py` (`CampaignService`) | Orchestrate domain and ports. Most behaviour changes belong here. |
| DTOs | `messages.py` | `messages.py` | Pydantic models returned to front-ends. |
| Front-ends | `server.py`, `job_process.py` | `cli.py` | Entry points. Each builds its own `BehaveService` from the real adapters; the copies are deliberate, and `tests/behave_mcp/test_reconcile.py` checks they act alike. The handlers in `server.py` and `cli.py` parse arguments, call one service method and serialise the result; `job_process.py` is the process each job runs under. |
| Config | `config.py` | none | The server's own env vars (address, poll timeout) parsed once into `Settings`. The checkout's config is not here: it is `domain/config.py`, stored in `config.json`. |

Small collaborators are passed as plain callables, not ports: the clock (`now_utc`, `now`, `monotonic`), `sleep`, `new_job_id` and the git read (`repo_state`). Tests replace them with lambdas.

## Where a change goes

| Change | Put it in |
| --- | --- |
| A rule about units, attempts, campaign states, scheduling, classification or log parsing | the module for that concern in the package's `domain/` |
| How a result is shown to a caller or written for a reviewer | the package's views |
| A new kind of external interaction | a `Protocol` in `ports.py`, an adapter in `adapters.py`, and a fake for tests |
| What a tool or command does | the service method |
| A new MCP tool | a handler in `server.py` registered with the local `@tool`, plus the README and the agent skills |
| A new CLI command | a handler in `cli.py` over a `CampaignService` method |
| A new setting for jobs | `domain/config.py`, set with `behave-campaign config`, and the README's configuration section. Never an env var: a job must not depend on who starts it |
| A new server env var | `config.py` (`Settings` and its parser) and the README's environment section |
| A new path under the state directory | `domain/layout.py`, which names paths and reads no env vars. The caller resolves `MCP_STATE_DIR` and passes it in. |

Register MCP tools with the local `@tool` decorator in `server.py`, never with `@mcp.tool`. It returns one compact JSON text block and publishes no output schema, which keeps results and the tool list small.

## State on disk

Everything the server writes lives under its own checkout's `.mcp_server_behave/`, or under `$MCP_STATE_DIR` when it is set. A per-call `repo_root` chooses where behave runs and which feature files are read, never where files are written:

| Path | Holds | Written by |
| --- | --- | --- |
| `config.json` | the checkout's config (`CheckoutConfig`): max jobs, behave options; owner-only | `behave-campaign config` |
| `reconcile.lock` | held while a process reconciles | whoever reconciles |
| `jobs/<job_id>/request.json` | what to run (`JobRequest`), written once when queued | `BehaveService.start_scenario` |
| `jobs/<job_id>/job.json` | the launch record (`JobRecord`), written once while holding `job.lock` | `BehaveService.reconcile` |
| `jobs/<job_id>/exit.json` | the exit (`JobExit`), written once by rename | the job process |
| `jobs/<job_id>/job.lock` | held by the job process while the job lives; holds its PID | the job process |
| `jobs/<job_id>/kill` | left by a kill, for a job process still starting up | `kill_job` |
| `lanes/<pool>/<n>.lock` | held by the process of the job using that lane | the job process |
| `jobs/<job_id>/stdout.log` | combined stdout and stderr of the job | the behave subprocess |
| `jobs/<job_id>/report.json` | behave's JSON report | the behave subprocess |
| `campaigns/<id>/campaign.json` | the header and the units in scope, written once | `CampaignService` (create) |
| `campaigns/<id>/records.jsonl` | every later record, append only; a torn last line is ignored and cut off by the next append | `CampaignService`, under `reconcile.lock` |
| `exports/<id>.tar.gz` | the evidence bundle | CLI `export` |

Only a campaign's intent is written, and events are never stored: `domain/feed.py` derives them from the records on every read. Its unit states, progress and named state are rebuilt by replaying its record file through `domain/state.py` (`reduce_units`, `progress`, `state_of`).

## Process model

- A server process serves MCP over streamable HTTP at `http://MCP_HOST:MCP_PORT/mcp`. FastMCP runs tool calls on worker threads. Several servers and CLIs may share a state directory.
- Nothing long-lived drives jobs. `BehaveService.reconcile` starts queued jobs on free lanes, under `reconcile.lock`, from files alone. It runs after every tool call (in the `@tool` decorator), every 5 s in the server, after every CLI command, and in a job's own process after it records its exit and lets go of its lane.
- Campaigns have no driver either. `CampaignService.reconcile` brings every campaign up to date -- records ended jobs, queues queueable units up to each campaign's max jobs -- and then reconciles the job queue, all under one hold of `reconcile.lock`. Every reconcile caller runs it, including the job process, which is why `behave_mcp.job_process` may import `behave_campaign`. Any number of campaigns run at once.
- `server.py` holds one `BehaveService` and one `JsonlCampaignStore`, shared so a reader waiting for events is woken by this process's appends. `CampaignService` is built per call, with `campaign_jobs()` as its job runner. Nothing about a job or a campaign is held in memory.
- Each behave job runs under `behave_mcp/job_process.py`, in its own session. The job process holds the job's lane lock (`lanes/<pool>/<n>.lock`) and `jobs/<id>/job.lock` for its whole life, runs `tox -e behave` as a child with a parent-death signal and stdin from `/dev/null`, and writes `exit.json` when tox exits. A job's state is read off those files and locks, from any process: a killed job is interrupted, never failed. `kill_job` sends SIGTERM to the job process, which passes it to tox's process group.

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

The test tree mirrors the source tree, so a failing file names the module that broke. The tests of `behave_campaign/domain/schedule.py` are in `tests/behave_campaign/domain/test_schedule.py`, and so on for every module:

| Directory | Holds |
| --- | --- |
| `tests/behave_mcp/` | one file per `behave_mcp` module, with `domain/` mirrored as a directory, plus `test_golden.py` for on-disk byte shapes and `test_startup.py` for configuration at import |
| `tests/behave_campaign/` | one file per `behave_campaign` module, with `domain/` and `views/` mirrored as directories |
| `tests/mcp_surface/` | the tools over the real MCP protocol, and the e2e tests |

A test file holds the tests of one module. Helpers and constants it needs are defined in it, so the file reads on its own.

Each directory is a Python package, so test files with the same name can coexist. Every fake lives in `tests/conftest.py`, one per port, so tests across tiers share it; `make_service` builds a `BehaveService` over them, so a new port is added in one place. An autouse fixture points `MCP_STATE_DIR` at the test's own directory, because every front-end reconciles. The `behave-mcp-unit-tests`, `behave-mcp-integration-tests` and `behave-mcp-e2e-tests` skills describe what each tier tests.

## Checks

Run these from `tools/mcp_server_behave`. Every one must pass before a change is done:

```bash
uv sync --extra test --extra lint
uv run pytest -q -m "not e2e" --cov
uv run black --check behave_mcp behave_campaign tests
uv run isort --check-only behave_mcp behave_campaign tests
uv run flake8 behave_mcp behave_campaign tests
uv run mypy behave_mcp behave_campaign
uv run lint-imports
```

Coverage is a signal, not a gate: nothing fails on a number. The report lists the lines and branches each file misses. Read them for the files you changed, and either cover them or say in the commit why they stay uncovered.

`uv sync --extra lint` alone removes pytest from the environment. Always sync both extras together. This is a local project, so nothing runs these checks for you.

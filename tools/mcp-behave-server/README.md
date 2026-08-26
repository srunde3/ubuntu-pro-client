# MCP Behave Server

This package provides a host-side MCP server for running selected behave scenarios in this repository.

## What it does

The server exposes these MCP tools:

- `list_features` -- lightweight catalog of feature files.
  - Each entry: `path`, `title`, `scenario_count`, `requires_config`, `releases`, `machine_types`.
  - Optional `release`, `machine_type`, `tag`, `text` filters keep only features with at least one matching scenario.
- `describe_feature` -- full detail for a single feature (`feature_file` must be a path from `list_features`).
  - Returns its title, tags, required config, and every scenario: name, type, tags, required config, `Examples` column names, and the distinct `(release, machine_type)` combos it supports.
- `list_dimensions` -- every distinct `release` and `machine_type` (substrate) used across the whole suite, each with a count of scenarios that reference it.
  - Use it to discover valid filter values.
- `find_scenarios` -- reverse lookup across all features.
  - Optional `release`, `machine_type`, `tag`, and `text` (scenario-name substring) filters.
  - Returns matching `feature_file`, `scenario_name`, `type`, required config, and the combos satisfying the filters.
- `start_scenario` -- starts a behave scenario in the background.
  - Returns a `job_id`.
- `list_scenario_jobs` -- lists active jobs plus a bounded window of recently completed ones.
  - No known `job_id` or access to system processes required.
  - Merges in-memory state with jobs recovered from disk, including jobs still running after a server restart.
- `summarize_scenario_results` -- aggregates results across jobs.
  - Optional `job_ids`, `feature_file`, `scenario_name` (substring), `release`, `machine_type`, and `status` filters.
  - Returns `job_counts` (status totals), scenario-level pass/fail counts grouped `by_release` and `by_machine_type`, and a flattened `failures` list tagged with `job_id` and combo context (capped at `limit`, with `truncated` set when more exist).
  - Provides raw status/data only -- rerunning failed scenarios and judging flaky-vs-real failures is left to the caller.
- `wait_for_scenario_completion` -- waits for completion.
  - Returns a compact completion summary, or a timeout payload.
- `get_scenario_logs` -- returns a bounded tail of captured stdout logs for a job.
- `get_scenario_artifacts` -- returns disk artifact paths and metadata for a job.

Release and substrate values are derived by parsing each feature's Gherkin `Examples` tables with the `behave` library, so the catalog always reflects the current feature files (no hardcoded release/substrate lists).

For each started job, the server writes artifacts under `.mcp_behave_logs`:

- `<job_id>_stdout.log`: combined stdout/stderr stream
- `<job_id>_report.json`: behave JSON formatter output
- `<job_id>_meta.json`: machine-readable metadata (command, params, status, timestamps, artifact paths)
- `index.jsonl`: append-only per-job lifecycle events (`started`, `completed`)

It also exposes a health endpoint at `/healthz` for basic checks.

## Local usage

This server is run via `uvx` from an MCP client config -- see "Example MCP
client configuration" below for the one supported setup.

## Example MCP client configuration

The supported setup runs the server with `uvx`, pointed at your
`ubuntu-pro-client` checkout (a worktree works fine). `uvx` builds a
non-editable copy, so repo auto-detection doesn't work -- set
`UBUNTU_PRO_CLIENT_REPO` to that checkout explicitly (see `repo_root` under
[Environment variables](#environment-variables)):

```json
{
  "mcpServers": {
    "behave": {
      "command": "uvx",
      "args": [
        "--from",
        "/path/to/repo/tools/mcp-behave-server",
        "mcp-behave-server"
      ],
      "env": {
        "UBUNTU_PRO_CLIENT_REPO": "/path/to/repo"
      }
    }
  }
}
```

Other env vars (contract token, concurrency limits, etc.) go in the same
`env` block -- see [Environment variables](#environment-variables).

If you prefer keeping cache enabled, clear stale entries after package changes:

```bash
uv cache clean mcp-behave-server
```

## Testing

Run the package tests:

```bash
uv run pytest -q
```

Run only fast tests (exclude end-to-end long-running test):

```bash
uv run pytest -q -m "not e2e"
```

Run only the long-running end-to-end MCP test:

```bash
uv run pytest -q -m "e2e and long_running"
```

## Linting and type checking

This package is not covered by the repo-root `tox.ini` lint/type envs
(those only check `uaclient/ features/ lib/`), so it keeps its own
`lint` dependency group and runs these tools directly via `uv`:

```bash
uv sync --extra lint
uv run black --check behave_mcp tests
uv run isort --check-only behave_mcp tests
uv run flake8 behave_mcp tests
uv run mypy behave_mcp
```

`pro-client-features` is installed editable via a setuptools finder that
mypy's import resolution can't see on its own; `[tool.mypy] mypy_path`
in `pyproject.toml` points mypy at the source tree instead.

## Environment variables

The server forwards its **entire** environment to the behave subprocess.
Whatever env vars the MCP server itself is started with,
including things like `UACLIENT_BEHAVE_CONTRACT_TOKEN` and
`UACLIENT_BEHAVE_INSTALL_FROM`, are visible to the spawned `behave` process.
Treat the MCP server's environment as the behave subprocess's environment
when configuring an MCP client.

The server also reads these variables at startup:

- `MCP_ALLOW_CLOUD_MACHINE_TYPES`: defaults to disabled. Set to `1` (or `true`/`yes`/`on`) to allow cloud machine types (`aws.generic`, `gcp.generic`, `azure.generic`, `aws.pro`, `gcp.pro`, `azure.pro`).
- `MCP_MAX_PARALLEL_JOBS`: positive integer limit for concurrently running behave jobs. Defaults to `1` when unset. When the limit is reached, `start_scenario` fails fast with `status: capacity_exceeded`.
- `MCP_TRANSPORT`: one of `stdio` (default), `sse`, or `streamable-http`.
- `MCP_HOST`: host to bind when using the `sse`/`streamable-http` transports. Defaults to `127.0.0.1`. Ignored for `stdio`.
- `MCP_PORT`: port to bind when using the `sse`/`streamable-http` transports. Defaults to `8000`. Ignored for `stdio`.

Every tool also accepts a `repo_root` parameter:

- `repo_root`: the repository to run behave against. If a call omits it, the server falls back to `UBUNTU_PRO_CLIENT_REPO`, then to auto-detection -- which only works when running from an editable/in-place install (`uv run` from inside the package directory), not via `uvx --from`. **When using `uvx --from` (the documented usage), set `UBUNTU_PRO_CLIENT_REPO` or always pass `repo_root` explicitly.**

One more variable is read at the point a job starts, and can vary per-call:

- `MCP_LOG_DIR`: overrides where job artifacts (`*_stdout.log`, `*_report.json`, etc.) are written. Defaults to `<repo_root>/.mcp_behave_logs`.

## Safety constraints

- `feature_file` must be one of the paths returned by `list_features`.
- `machine_types` is required.
- Cloud machine types are blocked by default and require setting `MCP_ALLOW_CLOUD_MACHINE_TYPES=1`.
- Parallel behave starts are capped at `1` by default, and can be adjusted with `MCP_MAX_PARALLEL_JOBS`.

## TODOs

- Better incorporate the shared file parsing library. Treated as a totally external dep right now, which introduces unnecessary code duplication. I don't quite yet know how I want to architect this.
- Add way to kill jobs if they are known to be hanging
- Add different "install from" options. Continue defaulting to 'local'. Include git commit or other unique identifier for build for each option, and surface it as a `summarize_scenario_results` filter/grouping dimension once it exists.
- Improve the job recovery mechanism; it's a little verbose on logs.

Known limitation: job liveness after a server restart is determined by checking whether the recorded PID is still alive (`os.kill(pid, 0)`). If that PID has since been reused by an unrelated process, a dead job can be misreported as still running. This is considered an acceptable tradeoff for a local dev tool.

Investigate possible parallel execution issue:

> Stalled again on tox provisioning. The tox lock issue still exists when jobs run concurrently (even with different machine types / releases). Let me kill that job and check on the other one.

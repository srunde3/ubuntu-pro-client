# MCP Behave Server

This package provides a host-side MCP server for running selected behave scenarios in this repository.

## What it does

The server exposes these MCP tools:

- `list_features`: returns a lightweight catalog of feature files. Each entry has `path`, `title`, `scenario_count`, `requires_config`, and the `releases` and `machine_types` the feature covers. Optional `release`, `machine_type`, `tag`, and `text` filters keep only features with at least one matching scenario.
- `describe_feature`: returns full detail for a single feature (`feature_file` must be a path from `list_features`): its title, tags, required config, and every scenario with name, type, tags, required config, `Examples` column names, and the distinct `(release, machine_type)` combos it supports.
- `list_dimensions`: returns every distinct `release` and `machine_type` (substrate) used across the whole suite, each with a count of scenarios that reference it. Use it to discover valid filter values.
- `find_scenarios`: reverse lookup across all features by optional `release`, `machine_type`, `tag`, and `text` (scenario-name substring) filters. Returns matching `feature_file`, `scenario_name`, `type`, required config, and the combos satisfying the filters.
- `start_behave_scenario`: starts a behave scenario in the background and returns a `job_id`
- `list_scenario_jobs`: lists active jobs plus a bounded window of recently completed ones, without needing a known `job_id` or access to system processes. Merges in-memory state with jobs recovered from disk, including jobs still running after a server restart.
- `summarize_scenario_results`: aggregates results across jobs matching optional `job_ids`, `feature_file`, `scenario_name` (substring), `release`, `machine_type`, and `status` filters. Returns `job_counts` (status totals), scenario-level pass/fail counts grouped `by_release` and `by_machine_type`, and a flattened `failures` list tagged with `job_id` and combo context (capped at `limit`, with `truncated` set when more exist). Provides raw status/data only -- rerunning failed scenarios and judging flaky-vs-real failures is left to the caller.
- `wait_for_scenario_completion`: waits for completion and returns a compact completion summary or timeout payload
- `get_scenario_logs`: returns a bounded tail of captured stdout logs for a job
- `get_scenario_artifacts`: returns disk artifact paths and metadata for a job

Release and substrate values are derived by parsing each feature's Gherkin `Examples` tables with the `behave` library, so the catalog always reflects the current feature files (no hardcoded release/substrate lists).

For each started job, the server writes artifacts under `.mcp_behave_logs`:

- `<job_id>_stdout.log`: combined stdout/stderr stream
- `<job_id>_report.json`: behave JSON formatter output
- `<job_id>_combo.jsonl`: one JSON line per scenario with its resolved `(release, machine_type)` and status -- see below
- `<job_id>_meta.json`: machine-readable metadata (command, params, status, timestamps, artifact paths)
- `index.jsonl`: append-only per-job lifecycle events (`started`, `completed`)

It also exposes a health endpoint at `/healthz` for basic checks.

## Attributing results to a release/machine_type

A single job's behave invocation can cover multiple `releases` x `machine_types` at once (each Examples row runs as its own scenario), but the JSON report's scenario name for an Outline row is just an index (e.g. `"Check pro version -- @1.1 version"`), not labeled with the actual release/machine_type values. Behave itself never serializes that mapping into any built-in formatter/reporter output (checked the full formatter/reporter list in behave's own docs -- `json`, `json.pretty`, `plain`, `pretty`, `progress*`, `rerun`, `sphinx.steps`, `steps.*`, `tags`, `tags.location`, `junit`, `summary`; none of them carry it), but it's still sitting on the live `Scenario` object as `scenario._row` throughout the run -- including for skipped scenarios.

`start_behave_scenario` appends a small custom formatter, `features.behave_combo_formatter:ComboFormatter` (`features/behave_combo_formatter.py`, a `behave.formatter.base2.BaseFormatter2` subclass implementing `on_scenario_end`), to the behave command alongside the existing `-f json -o ... -f plain`. It writes one JSON line per scenario to `<job_id>_combo.jsonl`: `{"location": "path:line", "status": ..., "release": ..., "machine_type": ...}`, keyed by the same `location` the JSON report already uses. `summarize_scenario_results` joins the two by that key to attribute each scenario's result precisely (`precise: true` in `by_release`/`by_machine_type`/`failures`).

This is generated fresh by the exact behave process that ran, so it can never drift the way a separately-maintained snapshot could. Jobs whose `<job_id>_combo.jsonl` is missing or empty (e.g. run against a `features/` that predates `behave_combo_formatter.py`) fall back to attributing their results across every one of the job's declared `releases`/`machine_types` instead (`precise: false`).

**Note**: `features/behave_combo_formatter.py`, plus a `PYTHONPATH = {toxinidir}` `setenv` entry in `[testenv:behave]` in the repo-root `tox.ini`, must exist in whatever `repo_root` a job targets -- behave resolves `-f`/`-o` formatter arguments before it adds `features/` to `sys.path` itself, so the repo root needs to already be on `PYTHONPATH` for a dotted import like this to resolve at all. Both pieces are being contributed upstream as an independently useful change; until that lands (or until a target `repo_root` otherwise has them), `start_behave_scenario` will fail outright rather than degrade gracefully, since neither piece is checked for existence beforehand.

## Local usage

From the repository root, the package can be run directly with uvx without needing to change into the package directory:

```bash
uvx --from $(pwd)/tools/mcp-behave-server mcp-behave-server
```

If you are already inside the package directory, this also works:

```bash
uv run mcp-behave-server
```

## Example MCP client configuration

A minimal config entry for an MCP client looks like this:

```json
{
  "mcpServers": {
    "behave": {
      "command": "uvx",
      "args": [
        "--from",
        "/path/to/repo/tools/mcp-behave-server",
        "mcp-behave-server"
      ]
    }
  }
}
```

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

The server will forward a small allowlist of environment variables to the behave subprocess when present:

- `UACLIENT_BEHAVE_CONTRACT_TOKEN`
- `UACLIENT_BEHAVE_INSTALL_FROM`

The server also supports one MCP-only safety toggle:

- `MCP_ALLOW_CLOUD_MACHINE_TYPES`: defaults to disabled. Set to `1` (or `true`/`yes`/`on`) to allow cloud machine types (`aws.generic`, `gcp.generic`, `azure.generic`).
- `MCP_MAX_PARALLEL_JOBS`: positive integer limit for concurrently running behave jobs. Defaults to `1` when unset. When the limit is reached, `start_behave_scenario` fails fast with `status: capacity_exceeded`.

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

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
  - `limit` caps the matches (default 50), with `total` and `truncated` alongside; `limit_clamped` is set when a value above the server max was capped.
- `start_scenario` -- starts one unit in the background: one scenario on one release and one machine type.
  - Returns a `job_id`.
  - `feature_file`, `scenario_name`, `release` and `machine_type` are all required. `scenario_name` is a scenario's exact name, as `describe_feature` lists it, and the scenario must cover that release and machine type. The job runs that scenario's rows for that combo and no other scenario.
  - A selection that matches no unit is rejected, and so is one that matches more than one: two scenarios with the same name that both cover the combo, which behave would run together.
  - The job runs in its own session with no stdin, so `kill_job` stops it and everything it started.
  - Optional `install_from` controls where `ubuntu-pro-client` is installed from before the scenario runs (sets `UACLIENT_BEHAVE_INSTALL_FROM` for the behave subprocess). Allowed values: `local` (default), `archive`, `daily`, `staging`, `stable`, `proposed`.
  - Returns `repo`: the checkout's `root`, and its git `commit`, `branch` and `dirty` flag at launch time (the last three `null` when it isn't a git checkout), so results can be correlated back to the exact code tested. Also persisted on the job record.
- `list_scenario_jobs` -- lists active jobs plus a bounded window of recently ended ones (finished or interrupted).
  - No known `job_id` or access to system processes required.
  - Reads every job off disk, so a job started by another server, by the CLI, or before a restart is listed and its state is current.
  - Optional `limit` caps how many ended jobs are returned (most recent first). `total_ended` and `truncated` in the response tell you if older jobs were dropped. `limit` must be positive; values above the server max are silently capped, with `limit_clamped` set when that happens.
- `get_scenario_results` -- what came of each job: what it ran, `status` and `ok`, behave's own counts, and every failing step with its message.
  - One `JobResult` per job, most recently started first -- the same shape `wait_for_scenario_completion` returns for one job.
  - Optional `job_ids`, `feature_file`, `scenario_name` (substring), `release`, `machine_type`, and `status` filters; `limit` caps the jobs listed, with `total` and `truncated` alongside. `limit` must be positive; values above the server max are capped, with `limit_clamped` set.
- `wait_for_scenario_completion` -- waits for completion.
  - Returns the job's `JobResult` (plus `returncode` and artifact paths), or a timeout payload.
- `get_scenario_errors` -- what went wrong in a job, from its log: every traceback, hook error and failed assertion in the order they happened, each with its line range, the failing step above it, and the exception raised.
  - The first region is usually the cause and later ones its consequences; `errors` is capped with `errors_total` alongside, long regions and lines are elided.
  - `finished` says whether behave reached its summary; `summary` is that block; `tail` and `log_path` are there when the parser has nothing to say (a tox or pip failure before behave, a killed job).
- `get_scenario_logs` -- reads part of a job's stdout log, every line prefixed with its number.
  - With `pattern` (a regex): the matching lines from `start` on, each with `context` lines around it, grep-style, until the next window would exceed `lines`. `matches` counts every match whether or not it fit.
  - Without: `lines` lines from `start`, or the last `lines` when `start` is 0.
  - `truncated` says the `lines` budget cut something; `log_path` is the file itself.
- `get_scenario_artifacts` -- returns a job's file paths, which of them exist, its launch `record` and its `exit` (null until it ends).

Release and substrate values are derived by parsing each feature's Gherkin `Examples` tables with the `behave` library, so the catalog always reflects the current feature files (no hardcoded release/substrate lists).

Everything the server writes lives under one directory in the server's own
checkout, `.mcp_server_behave`, with a subdirectory per kind of artifact. A
call that names another checkout with `repo_root` runs there, but its files
still land here, so one server keeps one set of jobs and campaigns:

```
.mcp_server_behave/
  jobs/        one directory per behave job
  lanes/       one lock file per lane, held by the job using it
  campaigns/   one directory per campaign: its record and run lock
```

`MCP_STATE_DIR` moves the whole thing; there is no separate variable per kind.

For each started job, the server writes artifacts under `jobs/`:

- `<job_id>/job.json`: what the job was asked to do -- unit, command, install source, checkout and its git state, start time, artifact paths. Written once, at launch.
- `<job_id>/exit.json`: how it ended -- `returncode`, `ok`, `killed`, `finished_at`, and the `error` when tox could not start. Written once, by the job process; a job without one is running, or was interrupted.
- `<job_id>/job.lock`: held by the job process for as long as it lives, and holding its PID. See [How a job runs](#how-a-job-runs).
- `<job_id>/stdout.log`: combined stdout/stderr stream
- `<job_id>/report.json`: behave JSON formatter output

Both JSON files are written to a temporary file and renamed into place, so a reader never sees half of one. `list_scenario_jobs` lists every directory holding a `job.json`.

It also exposes a health endpoint at `/healthz` for basic checks.

## Campaigns

This project also ships a `behave_campaign` package: the durable record of which test
units are in scope for a verification run, what has been attempted, and what to
run next. The server's job history is a bounded window, so an SRU campaign that
runs for days is recorded there instead.

`create_campaign` plans one and stores it without starting anything;
`list_campaigns` and `campaign_status` report on them. A campaign's state is
its intent -- what the caller last asked for: `never_run`, `run` or `hold` --
plus its progress, read off its units. `control_campaign` takes two actions:

- `run` sets the intent to run. The server keeps up to the campaign's `lanes`
  behave jobs in flight in each pool, records every outcome, and fills a lane
  as soon as one frees, with no further calls needed to keep it moving.
- `pause` sets the intent to hold. No new job starts, and jobs in flight
  finish and are recorded. With `kill: true` the jobs in flight are killed
  instead: their attempts are recorded as interrupted, and their units run
  again on the next `run`.

The named states follow from the two: `created`, `running`, `finishing` (the
last jobs are in flight), `complete` (every unit has an outcome; nothing writes
this), `pausing` (on hold, jobs still draining) and `paused`. A complete
campaign is never closed: `retry_units` gives it work and sets it running
again.

Only one campaign schedules at a time, held by an advisory lock so a second
server cannot schedule it too. A paused or complete campaign releases it. The
CLI does not take the lock.

State written in an earlier layout -- flat job files, single-file campaigns,
or campaigns from before this state model -- is not read. Delete
`.mcp_server_behave` after upgrading.

`await_campaign_events` is how a caller follows a running campaign: it blocks
until something happens, returns every event after a cursor, and always
reports the campaign's state -- state, intent, progress, lanes in flight,
counts by unit state -- so an empty batch is still informative. Each change of
state is announced as `campaign.<state>`. Events are filtered by kind (`unit.failed`) or family
(`unit.*`), and a `unit.failed` event carries the failing steps so a failure
can be judged without fetching the job's report. `anomaly.*` events and
`job.overdue` are derived signals offered for judgement -- repeated skips,
one scenario failing across every release, a refused lane, a job running far
too long -- and nothing acts on them automatically.

`unit_history` lists every attempt at each selected unit -- the job it ran
as, what it installed from, and its outcome -- so how a unit got to where it
is can be read without scanning the event stream.

`retry_units` asks for another attempt at units that already had one, which
is the only way a non-passing unit is ever re-run. It always sets the
campaign's intent to run. `kill_job` kills a
job that has hung, so its lane is recorded and freed instead of held open.

The package is also usable on its own through the `behave-campaign` CLI,
which names campaigns the way the server does (`--campaign-id`) and reads
the same files; it shares this project's `pyproject.toml`, virtualenv, and
CI job. See
[behave_campaign/README.md](behave_campaign/README.md) for the record format,
the CLI, and how MCP job results map onto attempts.

## Running the server

The server serves MCP over streamable HTTP. Start it from
`tools/mcp_server_behave`, with the environment its behave jobs need:

```bash
UACLIENT_BEHAVE_CONTRACT_TOKEN=... uv run mcp-server-behave
```

It listens on `http://127.0.0.1:8000/mcp`; `MCP_HOST` and `MCP_PORT` change
the address, and `/healthz` answers `{"status": "ok"}`. It finds the checkout
it runs from on its own. Set `UBUNTU_PRO_CLIENT_REPO` to serve a different
checkout.

Then point an MCP client at it. For Claude Code:

```bash
claude mcp add --transport http behave http://127.0.0.1:8000/mcp
```

Other clients take the same URL, for example:

```json
{
  "mcpServers": {
    "behave": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
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

Run only the long-running end-to-end tests:

```bash
uv run pytest -q -m "e2e and long_running"
```

`tests/mcp_surface/test_campaign_e2e.py` holds the e2e tests that need no
contract token and no LXD. They run real campaigns over
`features/_version.feature`'s attached-version scenario, which skips for want
of `contract_token` config: one asserts that three behave subprocesses were in
flight at once, which proves the scheduler's concurrency is real rather than
faked, and one pauses with kill on a real job and runs the campaign to
completion. They finish in seconds.

## Linting and type checking

This package is not covered by the repo-root `tox.ini` lint/type envs
(those only check `uaclient/ features/ lib/`), so it keeps its own
`lint` dependency group and runs these tools directly via `uv`:

```bash
uv sync --extra lint
uv run black --check behave_mcp behave_campaign tests
uv run isort --check-only behave_mcp behave_campaign tests
uv run flake8 behave_mcp behave_campaign tests
uv run mypy behave_mcp behave_campaign
uv run lint-imports
```

`lint-imports` checks the import contracts in `pyproject.toml`: which
modules may import which, within and between the two packages.

## Environment variables

The server forwards its **entire** environment to the behave subprocess.
Whatever env vars the MCP server itself is started with,
including things like `UACLIENT_BEHAVE_CONTRACT_TOKEN` and
`UACLIENT_BEHAVE_INSTALL_FROM`, are visible to the spawned `behave` process.
Set what the jobs need in the environment you start the server in.

The server also reads these variables at startup:

- `MCP_MAX_LXD_JOBS`, `MCP_MAX_CLOUD_JOBS`, `MCP_MAX_WSL_JOBS`: how many
  behave jobs may run at once in each pool. Defaults are `1`, `0` and `1`.
  `0` disables the pool, so cloud machine types are off until
  `MCP_MAX_CLOUD_JOBS` is set. `MCP_MAX_WSL_JOBS` may only be `0` or `1`,
  because WSL jobs share one Windows host.

Each machine type draws on one pool:

| Pool | Machine types |
| --- | --- |
| `lxd` | `lxd-container`, `lxd-vm` |
| `cloud` | `aws.*`, `gcp.*`, `azure.*`, including `pro` and `pro-fips` |
| `wsl` | `wsl` |

A job holds one lane in the pool its `machine_type` draws on, for as long as
it runs. Lanes are lock files under `lanes/<pool>/`, so every server and CLI
on one state directory shares them. Each process reads its own limits: two
processes with different limits share the lower-numbered lanes, so the
effective limit is the larger. When the pool is full, `start_scenario` fails fast with `status: capacity_exceeded` and
`full_pools` naming each full pool with its limit and running count. Starting
the server with a removed variable (`MCP_MAX_PARALLEL_JOBS`,
`MCP_ALLOW_CLOUD_MACHINE_TYPES` or `MCP_TRANSPORT`) is an error that says
what to use instead.

Other startup variables:

- `MCP_HOST`: host to bind. Defaults to `127.0.0.1`.
- `MCP_PORT`: port to bind. Defaults to `8000`.

The catalog tools (`list_features`, `describe_feature`, `list_dimensions`,
`find_scenarios`), `start_scenario` and `create_campaign` accept a `repo_root`
parameter:

- `repo_root`: the checkout to read feature files from and run behave in. If a call omits it, the server falls back to `UBUNTU_PRO_CLIENT_REPO`, then to the checkout it runs from. It never moves where the server writes.

Tools that look up a job or a campaign by id take no `repo_root`: every job
and campaign lives in the server's state directory.

- `MCP_CAMPAIGN_POLL_TIMEOUT`: longest wait `await_campaign_events` may be
  asked for, in seconds. Defaults to `60`, and must be between 1 and 120 --
  the real ceiling is the MCP client's own request timeout, which varies per
  host. A call asking for longer is rejected rather than quietly shortened.

- `MCP_STATE_DIR`: where the server writes everything -- job artifacts under
  `jobs/`, campaign records under `campaigns/`. Defaults to
  `.mcp_server_behave` in the server's checkout (`UBUNTU_PRO_CLIENT_REPO`, or
  the checkout it runs from).

## Safety constraints

- `feature_file` must be one of the paths returned by `list_features`.
- A job is exactly one unit: `scenario_name`, `release` and `machine_type` are required.
- Cloud machine types are blocked by default and require setting `MCP_MAX_CLOUD_JOBS`.
- Concurrent jobs are capped per pool: one LXD job and one WSL job by default.

## TODOs

- Improve the job recovery mechanism; it's a little verbose on logs.

## How a job runs

Each job runs under its own job process, `python -m behave_mcp.job_process`,
in its own session, so the server's death or a signal to it never reaches the
job. The server starts the job process and hands it two locks: the job's lane
and `jobs/<id>/job.lock`. The job process holds both for its whole life, runs
tox as its child, and when tox exits writes `exit.json` and exits.

That is what makes a job's state readable from any process, after any crash:

| `exit.json` | `job.lock` held | state |
| --- | --- | --- |
| yes, `killed: false` | either | `finished` |
| yes, `killed: true` | either | `interrupted`, reason `killed` |
| no | yes | `running` |
| no | no | `interrupted`, reason `died`: the job process died without writing an exit |

An interrupted job has no result: it is never a failure or an error, and
nothing it did counts.

tox gets a parent-death signal, so it cannot outlive its job process.
`kill_job` sends SIGTERM to the job process, which passes it to tox's process
group and waits for it, so pycloudlib can delete what it made; the exit is
recorded with `killed: true`. A hard kill or a reboot can leave an instance behind: cleaning
those up is pycloudlib's business, not this tool's.

- Some sort of API config endpoint and tool - can expose to the agent any relevant and non-secret config values. For secret config values like the token and cloud creds, we can indicate if they're set so that the agent can at least correlate skipped tests with missing required config.
- Take a second look at the response shapes on each endpoint. See if we can reduce the default response size and add flags for more verbose info. It would be great to have a clean integration between the MCP results and the test tracker inputs. Cutting down on response size also reduces token usage.
- See if there are instructions or API changes that can fix the problem of an agent waiting around for *every* single test in the batch to finish (i.e., `wait_for_scenario_completion` finishes) before launching a new batch. It means the slowest item in the batch dictates the iteration interval. It might be as simple as requesting subagent execution for each item in the batch instead of main loop execution - this would be a skill change only.

### Introduce test campaign APIs

Natively integrate the test campaign into the MCP - the MCP will allow the agent to create and launch test campaigns and can run them under concurrency. The agent consumes a stream of events that it might need to react to (especially failures). Results are automatically updated internally without the agent needing to do any translation.

The agent becomes solely responsible for deciding if a campaign should continue, or if it's doomed due to infra instability, missing config, etc. The agent decides what to retry, and when.

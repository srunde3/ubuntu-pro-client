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
- `start_scenario` -- queues one unit to run in the background: one scenario on one release and one machine type.
  - Returns a `job_id` and its `state`: `running` when its pool had a free lane, else `queued` with `queue_position` (1 is next in its pool). A queued job starts by itself when a lane frees; see [How a job runs](#how-a-job-runs).
  - `feature_file`, `scenario_name`, `release` and `machine_type` are all required. `scenario_name` is a scenario's exact name, as `describe_feature` lists it, and the scenario must cover that release and machine type. The job runs that scenario's rows for that combo and no other scenario.
  - A selection that matches no unit is rejected, and so is one that matches more than one: two scenarios with the same name that both cover the combo, which behave would run together.
  - The job runs in its own session with no stdin, so `kill_job` stops it and everything it started.
  - Optional `install_from` controls where `ubuntu-pro-client` is installed from before the scenario runs (sets `UACLIENT_BEHAVE_INSTALL_FROM` for the behave subprocess). Allowed values: `local` (default), `archive`, `daily`, `staging`, `stable`, `proposed`.
  - The job's record holds `repo`: the checkout's `root`, and its git `commit`, `branch` and `dirty` flag when the job started (the last three `null` when it isn't a git checkout), so results can be correlated back to the exact code tested.
- `list_scenario_jobs` -- lists queued and running jobs plus a bounded window of recently ended ones (finished or interrupted), and `pools`: each pool's max jobs and busy lanes, which is why a job is queued.
  - No known `job_id` or access to system processes required.
  - Reads every job off disk, so a job started by another server, by the CLI, or before a restart is listed and its state is current.
  - Optional `limit` caps how many ended jobs are returned (most recent first). `total_ended` and `truncated` in the response tell you if older jobs were dropped. `limit` must be positive; values above the server max are silently capped, with `limit_clamped` set when that happens.
- `get_scenario_results` -- what came of each job: what it ran, `state` and `ok`, behave's own counts, and every failing step with its message.
  - One `JobResult` per job, most recently started first -- the same shape `wait_for_scenario_completion` returns for one job.
  - Optional `job_ids`, `feature_file`, `scenario_name` (substring), `release`, `machine_type`, and `state` filters; `limit` caps the jobs listed, with `total` and `truncated` alongside. `limit` must be positive; values above the server max are capped, with `limit_clamped` set.
- `wait_for_scenario_completion` -- waits for a queued or running job to end.
  - Returns the job's `JobResult` (plus `returncode` and artifact paths) with `state` `finished` or `interrupted` (with `reason` `killed` or `died`). If the wait runs out first, it returns the job as it stands, `queued` or `running`, with `timed_out: true`.
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
  config.json     this checkout's config; see [Configuration](#configuration)
  reconcile.lock  held while a process reconciles the queue
  jobs/           one directory per behave job
  lanes/          one lock file per lane, held by the job using it
  campaigns/      one directory per campaign: its record and run lock
```

`MCP_STATE_DIR` moves the whole thing; there is no separate variable per kind.

For each job, these files live under `jobs/`:

- `<job_id>/request.json`: what the job is to run -- unit, built command, install source, checkout, queue time. Written once, when it is queued.
- `<job_id>/job.json`: what the job ran with -- the request, plus its start time, the checkout's git state, every behave option in force (each secret shown only as `<set>`) and artifact paths. Written once, when it starts.
- `<job_id>/exit.json`: how it ended -- `returncode`, `ok`, `killed`, `finished_at`, and the `error` when tox could not start. Written once, by the job process; a job without one is running, or was interrupted.
- `<job_id>/job.lock`: held by the job process for as long as it lives, and holding its PID. See [How a job runs](#how-a-job-runs).
- `<job_id>/stdout.log`: combined stdout/stderr stream
- `<job_id>/report.json`: behave JSON formatter output

The JSON files are written to a temporary file and renamed into place, so a reader never sees half of one. `list_scenario_jobs` lists every directory holding a `request.json`.

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
`tools/mcp_server_behave`:

```bash
uv run mcp-server-behave
```

What the jobs need, such as a contract token, goes in the checkout's
[configuration](#configuration), not the server's environment.

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

## Configuration

Each checkout has one config, `config.json` in its state directory, set only
with `behave-campaign config`. Every process that starts jobs reads it, so a
job runs the same whoever starts it.

```bash
behave-campaign config                                   # show it
behave-campaign config set max_jobs.lxd 3
echo "$TOKEN" | behave-campaign config set behave.contract_token -
behave-campaign config unset behave.custom_ppa
```

- `max_jobs.<pool>`: how many behave jobs may run at once in that pool.
  Defaults are `lxd` 1, `cloud` 0, `wsl` 1. `0` disables the pool, so cloud
  machine types are off until `max_jobs.cloud` is set. `wsl` may only be `0`
  or `1`, because WSL jobs share one Windows host.
- `behave.<option>`: any option behave reads from `UACLIENT_BEHAVE_<OPTION>`
  (`features/environment.py`), such as `contract_token` or `custom_ppa`.
  `install_from`, `releases` and `machine_types` are set per job and cannot
  be configured. Secrets (the contract tokens, the staging service account
  password, the Landscape keys) are never shown: `config` prints them as
  `<set>`, and `-` reads the value from stdin so it stays out of your shell
  history. `config.json` is readable only by you.

A change applies to every job that starts after it, campaign jobs included.

A job gets no `UACLIENT_BEHAVE_*` variable from the process that starts it,
only those from the config, plus `UACLIENT_BEHAVE_INSTALL_FROM` from its own
request. The server warns at startup about any it was started with. behave
still reads `~/.config/protest.yaml` itself, underneath the config.

Each machine type draws on one pool:

| Pool | Machine types |
| --- | --- |
| `lxd` | `lxd-container`, `lxd-vm` |
| `cloud` | `aws.*`, `gcp.*`, `azure.*`, including `pro` and `pro-fips` |
| `wsl` | `wsl` |

A running job holds one lane in its pool. Lanes are lock files under
`lanes/<pool>/`, shared by every process on the state directory. Busy counts
every held lane, so lowering max jobs takes effect at once: nothing new
starts until busy falls below it.

## Environment variables

The server reads these at startup:

- `MCP_HOST`: host to bind. Defaults to `127.0.0.1`.
- `MCP_PORT`: port to bind. Defaults to `8000`.
- `MCP_CAMPAIGN_POLL_TIMEOUT`: longest wait `await_campaign_events` may be
  asked for, in seconds. Defaults to `60`, and must be between 1 and 120 --
  the real ceiling is the MCP client's own request timeout, which varies per
  host. A call asking for longer is rejected rather than quietly shortened.
- `MCP_STATE_DIR`: where everything is written -- the config, job artifacts
  under `jobs/`, campaign records under `campaigns/`. Defaults to
  `.mcp_server_behave` in the server's checkout (`UBUNTU_PRO_CLIENT_REPO`, or
  the checkout it runs from).

Starting the server with a removed variable (`MCP_MAX_LXD_JOBS`,
`MCP_MAX_CLOUD_JOBS`, `MCP_MAX_WSL_JOBS`, `MCP_MAX_PARALLEL_JOBS`,
`MCP_ALLOW_CLOUD_MACHINE_TYPES` or `MCP_TRANSPORT`) is an error that says
what to use instead.

The catalog tools (`list_features`, `describe_feature`, `list_dimensions`,
`find_scenarios`), `start_scenario` and `create_campaign` accept a `repo_root`
parameter:

- `repo_root`: the checkout to read feature files from and run behave in. If a call omits it, the server falls back to `UBUNTU_PRO_CLIENT_REPO`, then to the checkout it runs from. It never moves where the server writes.

Tools that look up a job or a campaign by id take no `repo_root`: every job
and campaign lives in the server's state directory.

## Safety constraints

- `feature_file` must be one of the paths returned by `list_features`.
- A job is exactly one unit: `scenario_name`, `release` and `machine_type` are required.
- Cloud machine types are blocked by default and require `behave-campaign config set max_jobs.cloud N`.
- Concurrent jobs are capped per pool: one LXD job and one WSL job by default.

## How a job runs

A job is queued first: `start_scenario` writes its `request.json` and then
reconciles. **Reconcile** is one short step, run under `reconcile.lock` by
whichever process calls it: it reads the config and every job's files, and
starts the oldest queued jobs in each pool on that pool's free lanes. Pools
are independent, so a full LXD pool never holds back a WSL job.

Nothing long-lived drives the queue. Reconcile runs:

- after every MCP tool call, and every 5 seconds in the server;
- after every `behave-campaign` command;
- in a job's own process, after it records its exit and lets go of its lane,
  so the next queued job takes that lane at once.

Any number of servers and CLIs may share one state directory: they
coordinate through the lock and the files, and they all read the same
config.

Each job runs under its own job process, `python -m behave_mcp.job_process`,
in its own session, so the death of whatever started it, or a signal to it,
never reaches the job. Reconcile takes a lane and `jobs/<id>/job.lock`,
writes `job.json` while holding the lock, starts the job process and hands
it both. The job process holds them while it runs tox as its child, and when
tox exits it writes `exit.json`.

That is what makes a job's state readable from any process, after any crash:

| `exit.json` | `job.json` | `job.lock` held | state |
| --- | --- | --- | --- |
| yes, `killed: false` | either | either | `finished` |
| yes, `killed: true` | either | either | `interrupted`, reason `killed` |
| no | no | either | `queued` |
| no | yes | yes | `running` |
| no | yes | no | `interrupted`, reason `died`: the job process died without writing an exit |

An interrupted job has no result: it is never a failure or an error, and
nothing it did counts.

A job that cannot be started at all finishes at once, `ok: false`, with the
reason in `error`. tox gets a parent-death signal, so it cannot outlive its
job process. `kill_job` on a queued job records it as killed before it
starts. On a running job it sends SIGTERM to the job process, which passes
it to tox's process group and waits for it, so pycloudlib can delete what it
made; the exit is recorded with `killed: true`. A hard kill or a reboot can
leave an instance behind: cleaning those up is pycloudlib's business, not
this tool's.

## TODOs

- Some sort of API config endpoint and tool - can expose to the agent any relevant and non-secret config values. For secret config values like the token and cloud creds, we can indicate if they're set so that the agent can at least correlate skipped tests with missing required config.
- Take a second look at the response shapes on each endpoint. See if we can reduce the default response size and add flags for more verbose info. It would be great to have a clean integration between the MCP results and the test tracker inputs. Cutting down on response size also reduces token usage.

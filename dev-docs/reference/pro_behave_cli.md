# `pro-behave` CLI reference

`pro-behave` runs behave jobs, plans and runs campaigns -- the record of which test units are
in scope, what has been attempted, and what each attempt established -- and
holds the checkout's config, which every job runs with. `pro-behave mcp`
serves the same commands as MCP tools. A unit is one scenario for one release
on one machine type.

Run it from `tools/pro-behave`:

```bash
uv sync --extra test
uv run pro-behave <noun> <verb> [options]
```

Commands are grouped by what they act on: `job`, `campaign`, `feature`,
`scenario`, `dimension` and `config`. Each is the same command as its MCP
tool, with the same response shape:

| Command | MCP tool |
| --- | --- |
| `job run` | `run_job` |
| `job list` | `list_jobs` |
| `job results` | `job_results` |
| `job wait` | `wait_for_job` |
| `job errors` | `job_errors` |
| `job logs` | `job_logs` |
| `job artifacts` | `job_artifacts` |
| `job kill` | `kill_job` |
| `campaign create` | `create_campaign` |
| `campaign list` | `list_campaigns` |
| `campaign run`, `campaign pause` | `control_campaign` |
| `campaign retry` | `retry_units` |
| `campaign status` | `campaign_status` |
| `campaign history` | `unit_history` |
| `campaign events` | `await_campaign_events` |
| `campaign export` | `export_campaign` |
| `feature list` | `list_features` |
| `feature describe` | `describe_feature` |
| `scenario find` | `find_scenarios` |
| `dimension list` | `list_dimensions` |

`campaign units` has no tool; `config` has no tool, because only the user
changes the config. `mcp` starts the server; see [mcp](#mcp).

Every command prints one JSON document to stdout and exits `0`;
`campaign units` and `campaign history` also take `--format csv`. An invalid
argument, an unknown value, a missing job or campaign, or an unreadable file
prints a message to stderr and exits `2`. `campaign events --follow` prints
one document per batch, one per line.

After every command, the CLI reconciles the checkout: it brings every
campaign up to date and starts any queued job whose pool has a free lane,
as an MCP tool call does.

Every command takes `--repo-root DIR`, the `ubuntu-pro-client` checkout
whose feature files and state it uses. It defaults to
`$UBUNTU_PRO_CLIENT_REPO`. The state directory is
`<repo-root>/.pro-behave/`, or `$PRO_BEHAVE_STATE_DIR` when it is set. That
is the `behave` MCP server's own state directory when `--repo-root` is the
checkout the server serves, or when both share `PRO_BEHAVE_STATE_DIR`.
`--campaign-id ID` names a campaign there.

Unit states: `unattempted`, `in_flight`, `passed`, `failed`, `skipped`,
`error`.

Campaign states: `created`, `running`, `finishing`, `complete`, `pausing`,
`paused`. Each response that reports a campaign carries its `state`, its
`intent` (`never_run`, `run` or `hold`) and its `progress` (`untouched`,
`open`, `finishing` or `settled`).

Job states: `queued`, `running`, `finished`, `interrupted`.

Install sources: `local`, `archive`, `daily`, `staging`, `stable`,
`proposed`.

## job run

Queue one unit's job, and start it at once if its pool has a free lane.

```bash
uv run pro-behave job run FEATURE SCENARIO --release NAME \
  --machine-type NAME [--install-from SOURCE] [--wait]
```

* `FEATURE`: A path that `feature list` returns.
* `SCENARIO`: A scenario's exact name, as `feature describe` lists it, that
  covers the release and machine type.
* `--release NAME`, `--machine-type NAME`: Required.
* `--install-from SOURCE`: Defaults to `local`.
* `--wait`: Wait for the job to end, as `job wait` does, and print that
  instead.
* `--max-wait SECONDS`, `--interval SECONDS`: For `--wait`. Default `1800`
  and `5`.

## job list

Queued and running jobs, then the most recently ended ones, and each pool's
max jobs and busy lanes.

* `--limit N`: Most ended jobs to list. Defaults to `20`.

## job results

What came of each job: its state and, once it finished, its result: the
outcome, the reason, each of the unit's instances and the failing steps.

* `--job-id ID`: Only this job. Repeatable.
* `--feature PATH`, `--scenario NAME` (an exact name), `--release NAME`,
  `--machine-type NAME`, `--state STATE`: Filters.
* `--limit N`: Defaults to `20`.

## job wait, errors, logs, artifacts, kill

Each takes a `JOB_ID`.

* `job wait JOB_ID`: Wait for the job to end. A wait that runs out returns
  the job as it stands, with `timed_out: true`. `--max-wait SECONDS` and
  `--interval SECONDS` default to `1800` and `5`.
* `job errors JOB_ID`: What went wrong, from the job's log.
* `job logs JOB_ID`: Part of the job's log, every line numbered. `--lines N`
  (default `100`), `--start N`, `--pattern REGEX` and `--context N` (default
  `3`).
* `job artifacts JOB_ID`: The job's files, its request, record and result.
* `job kill JOB_ID`: Kill a queued or running job. It ends interrupted,
  never as a failure.

## feature list, feature describe, scenario find, dimension list

The feature catalogue, read from the checkout's feature files.

* `feature list`: Every feature file, with `--release`, `--machine-type`
  and `--tag` filters.
* `feature describe FEATURE`: One feature's scenarios and the combos each
  covers.
* `scenario find`: Scenarios across every feature, with the same filters
  and `--limit N` (default `50`).
* `dimension list`: Every release and machine type in use, each with a
  scenario count.

## campaign create

Create a campaign from the feature files. Units are built from the
combinations each scenario supports, narrowed by the filters. A campaign can
be created only once.

Required arguments:

* One of:
  * `--campaign-id ID`: The campaign, by the name the server uses.
  * `--campaign DIR`: A campaign directory anywhere; its id is the
    directory's name.
* `--repo-root DIR`: An `ubuntu-pro-client` checkout holding `features/`,
  whose state holds the campaign. Defaults to `$UBUNTU_PRO_CLIENT_REPO`.

Optional arguments:

* `--install-from SOURCE`: Install source every job in the campaign uses.
  Defaults to `local`.
* `--max-lxd-jobs N`: How many of the campaign's LXD jobs (`lxd-container`,
  `lxd-vm`) may be in flight at once. Defaults to `1` when the scope has LXD
  units.
* `--max-cloud-jobs N`: The same for cloud jobs (`aws.*`, `gcp.*`,
  `azure.*`). Defaults to `1` when the scope has cloud units.
* `--max-wsl-jobs N`: The same for WSL jobs, at most `1`. Defaults to `1`
  when the scope has WSL units.
* `--feature PATH`: Only this feature file. Repeatable.
* `--scenario NAME`: Only this exact scenario name. Repeatable.
* `--release NAME`: Only this release. Repeatable.
* `--machine-type NAME`: Only this machine type. Repeatable.

Omit every filter to cover every feature file.

```bash
uv run pro-behave campaign create --campaign-id 1234567 --repo-root ../.. \
  --release jammy --release noble --machine-type lxd-vm \
  --install-from proposed --max-lxd-jobs 4
```

Errors: an unknown release, machine type, or feature; a campaign that already
exists; no unit matching the filters; max jobs for a pool no unit in
scope draws on; fewer than one for a pool, or more than one for `wsl`; max
jobs above the checkout's `max_jobs.<pool>`, or units in a disabled pool; a
unit that two scenarios of the same name both cover; a campaign id with
characters other than letters, digits, `.`, `_` and `-`.

## campaign list, run, pause, retry

* `campaign list`: Every campaign on the checkout.
* `campaign run --campaign-id ID`: Set the intent to run, and queue what
  the campaign's max jobs allow at once.
* `campaign pause --campaign-id ID [--kill]`: Set the intent to hold. Jobs
  in flight finish, or with `--kill` are killed and their units recorded as
  interrupted.
* `campaign retry --campaign-id ID [--reason TEXT]`: Ask for another
  attempt at units that already had one, and set the intent to run. With no
  `--state`, it selects the failed, skipped and errored units. Takes the
  same filters as `campaign status`.

`run`, `pause` and `retry` need the checkout, so they refuse a campaign
named with `--campaign DIR` alone.

## campaign status

Report a campaign's header, state, jobs in flight in each pool, in-flight units,
and problem units.

Required arguments:

* One of:
  * `--campaign-id ID`: The campaign, by the name the server uses.
  * `--campaign DIR`: A campaign directory anywhere; its id is the
    directory's name.

Optional arguments:

* `--repo-root DIR`: The checkout whose state holds the campaign, for
  `--campaign-id`. Defaults to `$UBUNTU_PRO_CLIENT_REPO`.
* `--problems N`: List at most `N` problem rows. Defaults to `2000`.
* `--group-by unit|scenario`: List problems one unit per row, or one
  scenario per row with its units by state. Defaults to `unit`.
* `--feature PATH`: Only this feature file. Repeatable.
* `--scenario NAME`: Only this exact scenario name. Repeatable.
* `--release NAME`: Only this release. Repeatable.
* `--machine-type NAME`: Only this machine type. Repeatable.
* `--state STATE`: Only units in this state. Repeatable.

Filters narrow which units are counted and listed.

```bash
uv run pro-behave campaign status --campaign-id 1234567 --group-by scenario
uv run pro-behave campaign status --campaign-id 1234567 --release jammy
```

## campaign units

List every selected unit with its state, last `job_id` and
`attempt_count`.

Required arguments:

* One of:
  * `--campaign-id ID`: The campaign, by the name the server uses.
  * `--campaign DIR`: A campaign directory anywhere; its id is the
    directory's name.

Optional arguments:

* `--repo-root DIR`: The checkout whose state holds the campaign, for
  `--campaign-id`. Defaults to `$UBUNTU_PRO_CLIENT_REPO`.
* `--limit N`: Most units to list. Defaults to `200`; the maximum is `2000`.
* `--format json|csv`: Defaults to `json`. CSV is one row per unit with the
  columns `feature, scenario, release, machine_type, state, job_id,
  attempt_count`; a cut list is reported on stderr.
* `--feature PATH`: Only this feature file. Repeatable.
* `--scenario NAME`: Only this exact scenario name. Repeatable.
* `--release NAME`: Only this release. Repeatable.
* `--machine-type NAME`: Only this machine type. Repeatable.
* `--state STATE`: Only units in this state. Repeatable.

```bash
uv run pro-behave campaign units --campaign-id 1234567 --state failed \
  --format csv > failed.csv
```

## campaign history

List every attempt at each selected unit, oldest first: `job_id`,
`install_from`, `started_at`, `outcome`, `finished_at`, `interrupted`.

Required arguments:

* One of:
  * `--campaign-id ID`: The campaign, by the name the server uses.
  * `--campaign DIR`: A campaign directory anywhere; its id is the
    directory's name.

Optional arguments:

* `--repo-root DIR`: The checkout whose state holds the campaign, for
  `--campaign-id`. Defaults to `$UBUNTU_PRO_CLIENT_REPO`.
* `--limit N`: Most units to list. Defaults to `200`; the maximum is `2000`.
* `--format json|csv`: Defaults to `json`. CSV is one row per attempt with
  the columns `feature, scenario, release, machine_type, state, attempt,
  job_id, install_from, started_at, outcome, finished_at, interrupted`; a
  unit never attempted is one row with the attempt columns empty.
* `--feature PATH`: Only this feature file. Repeatable.
* `--scenario NAME`: Only this exact scenario name. Repeatable.
* `--release NAME`: Only this release. Repeatable.
* `--machine-type NAME`: Only this machine type. Repeatable.
* `--state STATE`: Only units in this state. Repeatable.

```bash
uv run pro-behave campaign history --campaign-id 1234567 \
  --feature features/cli/attach.feature
```

## campaign events

Read the events the campaign's records announce. Events are derived from the
records on every read, so they need no server and number the same way
everywhere. Each response carries the campaign's `state`, `intent`, `progress`, `in_flight`
and `counts` as well as the events.

Required arguments:

* One of:
  * `--campaign-id ID`: The campaign, by the name the server uses.
  * `--campaign DIR`: A campaign directory anywhere; its id is the
    directory's name.

Optional arguments:

* `--repo-root DIR`: The checkout whose state holds the campaign, for
  `--campaign-id`. Defaults to `$UBUNTU_PRO_CLIENT_REPO`.
* `--since-seq N`: Return events numbered above `N`. Defaults to `0`, all
  events. Pass a response's `next_seq` to continue.
* `--kinds KIND`: An event kind (`unit.failed`), a family (`unit.*`), or a
  preset. Repeatable. Defaults to everything.
* `--limit N`: Most events per response. Defaults to `100`; the maximum is
  `1000`.
* `--follow`: Keep printing responses, one JSON document per line, until the
  campaign is `paused` or `complete`. Each poll reconciles first, so
  following a campaign from a terminal keeps it moving when no server runs.
* `--interval SECONDS`: Poll interval with `--follow`. Defaults to `5`.

Families: `campaign`, `job`, `unit`, `anomaly`. Presets: `actionable`
(every non-passing outcome, `anomaly.*`, `job.overdue`, `campaign.*`) and
`*` (everything).

```bash
uv run pro-behave campaign events --campaign-id 1234567 \
  --kinds actionable --follow
```

Errors: an unknown kind, family, or preset.

## campaign export

Write a complete campaign's evidence bundle as a `.tar.gz`, for attaching to
an SRU bug. Prints the bundle's path, the counts by state, how many jobs were
bundled, and any job files that were missing.

Required arguments:

* One of:
  * `--campaign-id ID`: The campaign, by the name the server uses.
  * `--campaign DIR`: A campaign directory anywhere; its id is the
    directory's name.
* `--repo-root DIR`: The checkout whose state holds the campaign and its job
  files. Defaults to `$UBUNTU_PRO_CLIENT_REPO`.

Optional arguments:

* `--output PATH`: Where to write the bundle. Defaults to
  `<state dir>/exports/<id>.tar.gz`.

The bundle unpacks to one directory named after the campaign:

| Path | Contents |
| --- | --- |
| `SUMMARY.md` | Counts and scope, then every unit by feature, scenario and release: passed units, then the rest with their state and job id |
| `export.json` | Scope, install source, max jobs, state, counts |
| `units.csv` | One row per unit: `feature, scenario, release, machine_type, state, job_id, finished_at` |
| `jobs/<job_id>/` | `job.json`, `result.json`, `report.json`, `stdout.log` for every attempt, including superseded ones |
| `audit/campaign.json` | The campaign's header and units, as stored |
| `audit/records.jsonl` | The campaign's record log, as stored |

Each unit appears once, in the state its attempts ended in; the summary never
shows attempt counts. Repo state is left out of the review files, because
nothing ties a campaign's jobs to one commit; the raw records under `jobs/`
and `audit/` still carry it. Exporting the same campaign twice gives
identical bytes.

```bash
uv run pro-behave campaign export --campaign-id 1234567
```

Errors: a campaign that is not `complete`, naming its jobs in flight, and an output
path that already exists. Missing job files are not an error; they are listed in the output
and marked `files missing` in the summary.

## config

Show or change the checkout's config: how many jobs each pool may run at
once, and the behave options every job runs with. A change applies to every
job that starts after it, campaign jobs included.

```bash
uv run pro-behave config [--repo-root DIR] [show]
uv run pro-behave config [--repo-root DIR] set KEY VALUE
uv run pro-behave config [--repo-root DIR] unset KEY
```

Prints each pool's `max_jobs` and `busy` lanes, and the behave options, each
secret as `<set>`.

Keys:

* `max_jobs.lxd`, `max_jobs.cloud`, `max_jobs.wsl`: How many jobs the pool
  may run at once. Defaults are `1`, `0` and `1`; `0` disables the pool.
  `max_jobs.wsl` is at most `1`. `unset` returns the default.
* `behave.<option>`: An option behave reads from `UACLIENT_BEHAVE_<OPTION>`,
  such as `contract_token` or `custom_ppa`. `install_from`, `releases` and
  `machine_types` are set per job and are refused. `unset` drops the
  option.

A `VALUE` of `-` is read from stdin, so a secret stays out of shell history:

```bash
echo "$TOKEN" | uv run pro-behave config set behave.contract_token -
```

The config is `config.json` in the state directory, readable only by its
owner.

## mcp

```bash
uv run pro-behave mcp
```

Serves the MCP tools over streamable HTTP at
`http://PRO_BEHAVE_MCP_HOST:PRO_BEHAVE_MCP_PORT/mcp` (default
`http://127.0.0.1:8000/mcp`) until stopped, reconciling every 5 seconds and
after every tool call. It prints no JSON and does not reconcile when it
exits. The server's environment variables are listed in the
[tool's README](../../tools/pro-behave/README.md#environment-variables).

## Files

Each campaign is one directory, `campaigns/<id>/`:

| File                        | Written by                               | Contents                          |
| --------------------------- | ---------------------------------------- | --------------------------------- |
| `campaign.json`             | `create`, once                           | The header and the units in scope |
| `records.jsonl`             | `run`, `pause`, `retry`, every reconcile | Every later record, append-only   |
| `../../exports/<id>.tar.gz` | `export`                                 | The evidence bundle, by default   |

A crash mid-append can leave part of a line at the end of `records.jsonl`.
Reading ignores it, and the next append cuts it off.


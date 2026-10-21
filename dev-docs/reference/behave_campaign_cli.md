# `behave-campaign` CLI reference

`behave-campaign` creates, inspects and exports a behave test campaign: the
record of which test units are in scope, what has been attempted, and what
each attempt established. A unit is one scenario for one release on one
machine type. The `behave` MCP server runs a campaign and records every
attempt; the CLI does neither.

Run it from `tools/mcp_server_behave`:

```bash
uv sync --extra test
uv run behave-campaign <command> [options]
```

Every command prints one JSON document to stdout and exits `0`; `units` and
`history` also take `--format csv`. An invalid argument, an
unknown value, a missing campaign, or an unreadable file prints a message to
stderr and exits `2`. JSON shapes match the `behave` MCP server's campaign
tools.

`--campaign-id` resolves to `<repo-root>/.mcp_server_behave/campaigns/ID/`,
or `$MCP_STATE_DIR/campaigns/ID/` when `MCP_STATE_DIR` is set -- the same
file the `behave` MCP server uses for that id. `--repo-root` defaults to
`$UBUNTU_PRO_CLIENT_REPO`.

Unit states: `unattempted`, `in_flight`, `passed`, `failed`, `skipped`,
`error`.

Campaign states: `created`, `running`, `finishing`, `complete`, `pausing`,
`paused`. Each response that reports a campaign carries its `state`, its
`intent` (`never_run`, `run` or `hold`) and its `progress` (`untouched`,
`open`, `finishing` or `settled`).

Install sources: `local`, `archive`, `daily`, `staging`, `stable`,
`proposed`.

## dimensions

List the releases and machine types the feature files can run, each with a
scenario count.

Required arguments:

* `--repo-root DIR`: An `ubuntu-pro-client` checkout holding `features/`.
  Defaults to `$UBUNTU_PRO_CLIENT_REPO`.

```bash
uv run behave-campaign dimensions --repo-root ../..
```

## create

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
* `--lxd-lanes N`: LXD jobs (`lxd-container`, `lxd-vm`) a runner may keep
  in flight. Defaults to `1` when the scope has LXD units.
* `--cloud-lanes N`: Cloud jobs (`aws.*`, `gcp.*`, `azure.*`) a runner may
  keep in flight. Defaults to `1` when the scope has cloud units.
* `--wsl-lanes N`: WSL jobs a runner may keep in flight, at most `1`.
  Defaults to `1` when the scope has WSL units.
* `--feature PATH`: Only this feature file. Repeatable.
* `--scenario NAME`: Only this exact scenario name. Repeatable.
* `--release NAME`: Only this release. Repeatable.
* `--machine-type NAME`: Only this machine type. Repeatable.

Omit every filter to cover every feature file.

```bash
uv run behave-campaign create --campaign-id 1234567 --repo-root ../.. \
  --release jammy --release noble --machine-type lxd-vm \
  --install-from proposed --lxd-lanes 4
```

Errors: an unknown release, machine type, or feature; a campaign that already
exists; no unit matching the filters; lanes for a pool no unit in
scope draws on; fewer than one lane for a pool, or more than one for `wsl`.

## status

Report a campaign's header, state, lanes busy in each pool, in-flight units,
and problem units.

Required arguments:

* One of:
  * `--campaign-id ID`: The campaign, by the name the server uses.
  * `--campaign DIR`: A campaign directory anywhere; its id is the
    directory's name.

Optional arguments:

* `--repo-root DIR`: The checkout whose state holds the campaign, for
  `--campaign-id`. Defaults to `$UBUNTU_PRO_CLIENT_REPO`.
* `--problems N`: List at most `N` problem rows. Defaults to all.
* `--group-by unit|scenario`: List problems one unit per row, or one
  scenario per row with its units by state. Defaults to `unit`.
* `--feature PATH`: Only this feature file. Repeatable.
* `--scenario NAME`: Only this exact scenario name. Repeatable.
* `--release NAME`: Only this release. Repeatable.
* `--machine-type NAME`: Only this machine type. Repeatable.
* `--state STATE`: Only units in this state. Repeatable.

Filters narrow which units are counted and listed.

```bash
uv run behave-campaign status --campaign-id 1234567 --group-by scenario
uv run behave-campaign status --campaign-id 1234567 --release jammy
```

## units

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
uv run behave-campaign units --campaign-id 1234567 --state failed \
  --format csv > failed.csv
```

## history

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
  unit never
  attempted is one row with the attempt columns empty.
* `--feature PATH`: Only this feature file. Repeatable.
* `--scenario NAME`: Only this exact scenario name. Repeatable.
* `--release NAME`: Only this release. Repeatable.
* `--machine-type NAME`: Only this machine type. Repeatable.
* `--state STATE`: Only units in this state. Repeatable.

```bash
uv run behave-campaign history --campaign-id 1234567 \
  --feature features/cli/attach.feature
```

## events

Read the events the campaign's records announce. Events are derived from the
record on every read, so they need no server and number the same way
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
  campaign is `paused` or `complete`.
* `--interval SECONDS`: Poll interval with `--follow`. Defaults to `5`.

Families: `campaign`, `lane`, `unit`, `anomaly`. Presets: `actionable`
(every non-passing outcome, `anomaly.*`, `job.overdue`, `campaign.*`) and
`*` (everything).

```bash
uv run behave-campaign events --campaign-id 1234567 \
  --kinds actionable --follow
```

Errors: an unknown kind, family, or preset.

## export

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
| `campaign.json` | Scope, install source, lanes, state, counts |
| `units.csv` | One row per unit: `feature, scenario, release, machine_type, state, job_id, finished_at` |
| `jobs/<job_id>/` | `job.json`, `exit.json`, `report.json`, `stdout.log` for every attempt, including superseded ones |
| `audit/campaign.json` | The campaign's header and units, as stored |
| `audit/records.jsonl` | The campaign's record log, as stored |

Each unit appears once, in the state its attempts ended in; the summary never
shows attempt counts. Repo state is left out of the review files, because
nothing ties a campaign's jobs to one commit; the raw records under `jobs/`
and `audit/` still carry it. Exporting the same campaign twice gives
identical bytes.

```bash
uv run behave-campaign export --campaign-id 1234567
```

Errors: a campaign that is not `complete`, naming its jobs in flight, and an output
path that already exists. Missing job files are not an error; they are listed in the output
and marked `files missing` in the summary.

## Files

Each campaign is one directory, `campaigns/<id>/`:

| File                          | Written by            | Contents                                   |
| ----------------------------- | --------------------- | ------------------------------------------ |
| `campaign.json`               | `create`, once        | The header and the units in scope          |
| `records.jsonl`                | the server            | Every later record, append-only            |
| `campaign.lock`               | the server            | Advisory lock while the campaign schedules |
| `../../exports/<id>.tar.gz`   | `export`              | The evidence bundle, by default            |

A crash mid-append can leave part of a line at the end of `records.jsonl`.
Reading ignores it, and the next append cuts it off.


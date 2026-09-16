# `behave-campaign` CLI reference

`behave-campaign` reads and writes a behave test campaign: the record of which
test units are in scope, what has been attempted, and what each attempt
established. A unit is one scenario for one release on one machine type.

Run it from `tools/mcp_server_behave`:

```bash
uv sync --extra test
uv run behave-campaign <command> [options]
```

Every command prints one JSON document to stdout and exits `0`. An invalid
argument, an unknown value, or an unreadable file prints a message to stderr
and exits `2`. Response shapes match the `behave` MCP server's campaign tools.

## Common options

| Option                   | Applies to                                   | Meaning                                              |
| ------------------------ | -------------------------------------------- | ---------------------------------------------------- |
| `--campaign FILE`        | all but `dimensions`                         | The campaign's JSON Lines file. Required.            |
| `--repo-root DIR`        | `dimensions`, `create`                       | An `ubuntu-pro-client` checkout holding `features/`. |
| `--feature PATH`         | `create`, `status`, `next`, `history`        | Only this feature file. Repeatable.                  |
| `--scenario NAME`        | same                                         | Only this exact scenario name. Repeatable.           |
| `--release NAME`         | same                                         | Only this release. Repeatable.                       |
| `--machine-type NAME`    | same                                         | Only this machine type. Repeatable.                  |
| `--state STATE`          | `status`, `next`, `history`                  | Only units in this state. Repeatable.                |

Unit states: `unattempted`, `running`, `passed`, `failed`, `skipped`,
`error`.

Install sources: `local`, `archive`, `daily`, `staging`, `stable`,
`proposed`.

## dimensions

List the releases and machine types the feature files can run, each with a
scenario count.

Required arguments:

* `--repo-root`

```bash
uv run behave-campaign dimensions --repo-root ../..
```

## create

Create a campaign from the feature files. Units are built from the
combinations each scenario supports, narrowed by the filters. A campaign file
can be created only once.

Required arguments:

* `--campaign`
* `--repo-root`

Optional arguments:

* `--campaign-id ID`: Label for the campaign, such as an SRU bug number.
  Defaults to the campaign file's name.
* `--install-from SOURCE`: Install source every job in the campaign uses.
  Defaults to `local`.
* `--max-lanes N`: Jobs a runner may keep in flight. Defaults to `1`.
* Unit filters (`--feature`, `--scenario`, `--release`, `--machine-type`).
  Omit all of them to cover every feature file.

```bash
uv run behave-campaign create --campaign 1234567.jsonl --repo-root ../.. \
  --campaign-id 1234567 --release jammy --release noble \
  --machine-type lxd-vm --install-from proposed --max-lanes 4
```

Errors: an unknown release, machine type, or feature; a campaign file that
already exists; no unit matching the filters.

## record

Append completed attempts.

Required arguments:

* `--campaign`
* `--install-from SOURCE`: Where the jobs installed `ubuntu-pro-client` from.

Optional arguments:

* `--input FILE`: JSON file to read. Defaults to `-`, standard input.
* `--from-mcp`: Read `[{"unit": {...}, "result": <MCP payload>}]` instead of
  plain attempts.

Plain attempt input:

```json
[
  {
    "feature": "features/cli/attach.feature",
    "scenario": "Attach command in a ubuntu machine",
    "release": "jammy",
    "machine_type": "lxd-vm",
    "outcome": "passed",
    "job_id": "2f0fbaef"
  }
]
```

`outcome` is one of `passed`, `failed`, `skipped`, `error`.

```bash
uv run behave-campaign record --campaign 1234567.jsonl \
  --install-from proposed --input attempts.json
```

Errors: a unit the campaign never planned; an unknown outcome; an MCP result
that is not a clean pass, failure, or skip; a job still in flight.

## status

Report a campaign's header, state, in-flight units, and problem units.

Required arguments:

* `--campaign`

Optional arguments:

* `--units N`: Also list up to `N` individual units. Defaults to none.
* `--problems N`: List at most `N` problem rows. Defaults to all.
* `--group-by unit|scenario`: List problems one unit per row, or one
  scenario per row with its units by state. Defaults to `unit`.
* Unit filters and `--state`. Narrow which units are counted and listed.

```bash
uv run behave-campaign status --campaign 1234567.jsonl --group-by scenario
uv run behave-campaign status --campaign 1234567.jsonl --state failed --units 50
```

## next

List the units to run next: never-attempted units first, then failed,
skipped and errored units.

Required arguments:

* `--campaign`

Optional arguments:

* `--limit N`: Defaults to `1`.
* Unit filters and `--state`.

```bash
uv run behave-campaign next --campaign 1234567.jsonl --limit 4
```

## history

List every attempt at each selected unit, oldest first: `job_id`,
`install_from`, `started_at`, `outcome`, `finished_at`.

Required arguments:

* `--campaign`

Optional arguments:

* `--limit N`: Most units to list. Defaults to `200`; the maximum is `2000`.
* Unit filters and `--state`.

```bash
uv run behave-campaign history --campaign 1234567.jsonl \
  --feature features/cli/attach.feature
```

## events

Read the events a running server has written beside the campaign file. Each
response carries the campaign's `lifecycle`, `lanes_busy` and `counts` as
well as the events.

Required arguments:

* `--campaign`

Optional arguments:

* `--since-seq N`: Return events numbered above `N`. Defaults to `0`, all
  events. Pass a response's `next_seq` to continue.
* `--kinds KIND`: An event kind (`unit.failed`), a family (`unit.*`), or a
  preset. Repeatable. Defaults to everything.
* `--limit N`: Most events per response. Defaults to `100`; the maximum is
  `1000`.
* `--follow`: Keep printing responses, one JSON document per line, until the
  campaign is complete or cancelled with no lane in flight.
* `--interval SECONDS`: Poll interval with `--follow`. Defaults to `5`.

Families: `campaign`, `lane`, `unit`, `anomaly`. Presets: `actionable`
(every non-passing outcome, `anomaly.*`, `lane.overdue`, `campaign.*`) and
`*` (everything).

```bash
uv run behave-campaign events --campaign 1234567.jsonl \
  --kinds actionable --follow
```

Errors: an unknown kind, family, or preset.

## Files

| File                          | Written by            | Contents                                   |
| ----------------------------- | --------------------- | ------------------------------------------ |
| `<id>.jsonl`                  | `create`, `record`, the server | The campaign record, append-only   |
| `<id>.events.jsonl`           | the server            | Numbered events; `events` reads it         |
| `<id>.lock`                   | the server            | Advisory lock while the campaign schedules |

The `behave` MCP server keeps its campaigns under
`<repo>/.mcp_server_behave/campaigns/`, or `$MCP_STATE_DIR/campaigns/`. Point
`--campaign` at a file there to inspect a server-run campaign.

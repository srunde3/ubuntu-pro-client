# `behave-campaign` CLI reference

`behave-campaign` reads and writes a behave test campaign: the record of which
test units are in scope, what has been attempted, and what each attempt
established. A unit is one scenario for one release on one machine type.

Run it from `tools/mcp_server_behave`:

```bash
uv sync --extra test
uv run behave-campaign <command> [options]
```

Every command prints one JSON document to stdout and exits `0`; `units`,
`next` and `history` also take `--format csv`. An invalid argument, an
unknown value, a missing campaign, or an unreadable file prints a message to
stderr and exits `2`. JSON shapes match the `behave` MCP server's campaign
tools.

`--campaign-id` resolves to `<repo-root>/.mcp_server_behave/campaigns/ID.jsonl`,
or `$MCP_STATE_DIR/campaigns/ID.jsonl` when `MCP_STATE_DIR` is set -- the same
file the `behave` MCP server uses for that id. `--repo-root` defaults to
`$UBUNTU_PRO_CLIENT_REPO`.

Unit states: `unattempted`, `running`, `passed`, `failed`, `skipped`,
`error`.

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
combinations each scenario supports, narrowed by the filters. A campaign file
can be created only once.

Required arguments:

* One of:
  * `--campaign-id ID`: The campaign, by the name the server uses.
  * `--campaign FILE`: A campaign file anywhere; its id is the file's name.
* `--repo-root DIR`: An `ubuntu-pro-client` checkout holding `features/`,
  whose state holds the campaign. Defaults to `$UBUNTU_PRO_CLIENT_REPO`.

Optional arguments:

* `--install-from SOURCE`: Install source every job in the campaign uses.
  Defaults to `local`.
* `--max-lanes N`: Jobs a runner may keep in flight. Defaults to `1`.
* `--feature PATH`: Only this feature file. Repeatable.
* `--scenario NAME`: Only this exact scenario name. Repeatable.
* `--release NAME`: Only this release. Repeatable.
* `--machine-type NAME`: Only this machine type. Repeatable.

Omit every filter to cover every feature file.

```bash
uv run behave-campaign create --campaign-id 1234567 --repo-root ../.. \
  --release jammy --release noble --machine-type lxd-vm \
  --install-from proposed --max-lanes 4
```

Errors: an unknown release, machine type, or feature; a campaign file that
already exists; no unit matching the filters.

## record

Append completed attempts.

Required arguments:

* One of:
  * `--campaign-id ID`: The campaign, by the name the server uses.
  * `--campaign FILE`: A campaign file anywhere; its id is the file's name.
* `--install-from SOURCE`: Where the jobs installed `ubuntu-pro-client` from.

Optional arguments:

* `--repo-root DIR`: The checkout whose state holds the campaign, for
  `--campaign-id`. Defaults to `$UBUNTU_PRO_CLIENT_REPO`.
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
uv run behave-campaign record --campaign-id 1234567 \
  --install-from proposed --input attempts.json
```

Errors: a unit the campaign never planned; an unknown outcome; an MCP result
that is not a clean pass, failure, or skip; a job still in flight.

## status

Report a campaign's header, state, in-flight units, and problem units.

Required arguments:

* One of:
  * `--campaign-id ID`: The campaign, by the name the server uses.
  * `--campaign FILE`: A campaign file anywhere; its id is the file's name.

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
  * `--campaign FILE`: A campaign file anywhere; its id is the file's name.

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

## next

List the units to run next: never-attempted units first, then failed,
skipped and errored units.

Required arguments:

* One of:
  * `--campaign-id ID`: The campaign, by the name the server uses.
  * `--campaign FILE`: A campaign file anywhere; its id is the file's name.

Optional arguments:

* `--repo-root DIR`: The checkout whose state holds the campaign, for
  `--campaign-id`. Defaults to `$UBUNTU_PRO_CLIENT_REPO`.
* `--limit N`: Most units to list. Defaults to `1`.
* `--format json|csv`: Defaults to `json`. CSV is one row per unit, the same
  columns as `units`.
* `--feature PATH`: Only this feature file. Repeatable.
* `--scenario NAME`: Only this exact scenario name. Repeatable.
* `--release NAME`: Only this release. Repeatable.
* `--machine-type NAME`: Only this machine type. Repeatable.
* `--state STATE`: Only units in this state. Repeatable.

```bash
uv run behave-campaign next --campaign-id 1234567 --limit 4
```

## history

List every attempt at each selected unit, oldest first: `job_id`,
`install_from`, `started_at`, `outcome`, `finished_at`.

Required arguments:

* One of:
  * `--campaign-id ID`: The campaign, by the name the server uses.
  * `--campaign FILE`: A campaign file anywhere; its id is the file's name.

Optional arguments:

* `--repo-root DIR`: The checkout whose state holds the campaign, for
  `--campaign-id`. Defaults to `$UBUNTU_PRO_CLIENT_REPO`.
* `--limit N`: Most units to list. Defaults to `200`; the maximum is `2000`.
* `--format json|csv`: Defaults to `json`. CSV is one row per attempt with
  the columns `feature, scenario, release, machine_type, state, attempt,
  job_id, install_from, started_at, outcome, finished_at`; a unit never
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

Read the events a running server has written beside the campaign file. Each
response carries the campaign's `lifecycle`, `lanes_busy` and `counts` as
well as the events.

Required arguments:

* One of:
  * `--campaign-id ID`: The campaign, by the name the server uses.
  * `--campaign FILE`: A campaign file anywhere; its id is the file's name.

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
  campaign is complete or cancelled with no lane in flight.
* `--interval SECONDS`: Poll interval with `--follow`. Defaults to `5`.

Families: `campaign`, `lane`, `unit`, `anomaly`. Presets: `actionable`
(every non-passing outcome, `anomaly.*`, `lane.overdue`, `campaign.*`) and
`*` (everything).

```bash
uv run behave-campaign events --campaign-id 1234567 \
  --kinds actionable --follow
```

Errors: an unknown kind, family, or preset.

## Files

| File                          | Written by            | Contents                                   |
| ----------------------------- | --------------------- | ------------------------------------------ |
| `<id>.jsonl`                  | `create`, `record`, the server | The campaign record, append-only   |
| `<id>.events.jsonl`           | the server            | Numbered events; `events` reads it         |
| `<id>.lock`                   | the server            | Advisory lock while the campaign schedules |


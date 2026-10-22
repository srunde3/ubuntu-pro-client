# Campaigns

Durable record of behave test units and attempts.

A job records one run. A campaign records which units are in scope, every
attempt at each, and what it established, over an SRU verification that runs
for days.

A **test unit** is one behave scenario for one release on one `machine_type`.

## Storage

One directory per campaign, `campaigns/<id>/`. `campaign.json` holds the
campaign record and the units in scope, written once at creation; `records.jsonl` holds
every later record, appended. The two are replayed in full to get the current
state, and they are the only thing stored: every event a watcher hears is
derived from them. A crash mid-append can leave part of a line at the end of
`records.jsonl`; reading ignores it and the next append cuts it off. Nine
record types:

| type | written when | says |
| --- | --- | --- |
| `campaign` | once, in `campaign.json` | how the campaign was built: scope, checkout, install source, max jobs |
| `plan` | once per unit, in `campaign.json` | this unit is in scope |
| `started` | a job is queued for the unit | which job, and what it installs from |
| `finished` | that job ends | what it established: `passed`, `failed`, `skipped` or `error`; the first failing steps; and, for a result that could not be classified, the `reason` |
| `interrupted` | the job is killed, or its process dies | the job ended without an outcome, and why: `killed` or `died` |
| `intent` | `run`, `pause` or `retry_units` | what the caller asked the campaign to do: `run` or `hold` |
| `retry` | `retry_units` | someone asked for another go at a unit |
| `starved` | the job service refuses to queue a unit, such as for a disabled pool | which pool, and why; once per episode, until a job is next queued in that pool |
| `overdue` | a job runs past the overdue threshold | which job, and for how long; once per job |

One try at a unit is one job, written as two records. Both are needed: the
`started` record is what keeps the unit in flight, including across a
restart. It is written before the job is queued, so a crash between the two
leaves an attempt whose job does not exist, which the next reconcile records
as interrupted and queues again, never a job that nobody records. Neither
is an attempt on its own -- an **attempt** is the pair, matched on `job_id`,
and that is what `attempts` and `attempt_count` report. A unit tried twice has
two attempts, not four.

`in_flight` is therefore not an outcome. A unit is in flight while an attempt
has neither a `finished` nor an `interrupted` record, which is a fact about the
attempt rather than a result the job reported. An interrupted attempt
established nothing: it closes the attempt and leaves the unit to run again.

The `campaign` header captures how the campaign was built, so it can be
recreated and audited later: the campaign id, the filters used, the checkout it
was built from (root, commit, branch, dirty), and when. The header also records the
`install_from` every job runs with and its `max_jobs` in each pool its units
draw on, how many of its jobs may be in flight at once; both are required, so
a campaign always says how it is meant to be run. Every record carries a
UTC timestamp, and every attempt records the install source the job ran with --
the evidence an SRU verification rests on.

A unit's state is derived, never stored:

- An attempt with neither a finish nor an interruption makes it `in_flight`,
  whatever came before, including a pass being retried.
- Otherwise any passing attempt makes and keeps the unit `passed`.
- Otherwise the latest finished attempt's outcome wins: `failed`, `skipped`
  or `error`.
- With no finished attempt, it is `unattempted`.

A unit is **queueable** -- reconcile may queue an attempt at it -- when it
is not in flight and it is unattempted, has a retry requested since its last
attempt, or its latest attempt was interrupted. A problem unit is not
queueable on its own.

## Scope

`create` builds units from the feature files themselves, parsed with
`pro_behave.jobs.parser` -- the same parser the job service uses to select
scenarios at run time, so a campaign covers exactly the combinations each scenario supports
rather than a Cartesian product.

`create` defines scope once, from repeatable `--release`, `--machine-type`,
`--feature`, and `--scenario` filters; omitting them all yields the full
campaign. `--campaign-id` is an opaque label -- for SRU work it is the
Launchpad bug number, but the record attaches no meaning to it.

## Usage

```bash
uv sync --extra test
export UBUNTU_PRO_CLIENT_REPO=../..   # or pass --repo-root on each command

# See which releases and machine types the feature files can run.
uv run pro-behave dimension list

# Create the campaign: "all the jammy lxd-vm tests". The id names the directory
# the way the server names it, under <repo>/.pro-behave/campaigns/.
uv run pro-behave campaign create --campaign-id 1234567 \
  --release jammy --machine-type lxd-vm

# Repeat a filter to cover several values, as a real SRU usually does.
uv run pro-behave campaign create --campaign-id 1234568 \
  --release bionic --release focal --release jammy \
  --release noble --release resolute --release stonking

# With no filters, the campaign covers every feature file.
uv run pro-behave campaign create --campaign-id 1234569

# Record the install source, and max jobs per pool. A campaign's max jobs
# are within the checkout's, so raise those first.
uv run pro-behave config set max_jobs.lxd 6
uv run pro-behave config set max_jobs.cloud 4
uv run pro-behave campaign create --campaign-id 1234570 \
  --install-from proposed --max-lxd-jobs 6 --max-cloud-jobs 4 --max-wsl-jobs 1

# Run it, and pause it. A server, if one is running, keeps it moving; so
# does following its events.
uv run pro-behave campaign run --campaign-id 1234567
uv run pro-behave campaign pause --campaign-id 1234567 --kill

# Inspect it.
uv run pro-behave campaign status --campaign-id 1234567 --state failed
uv run pro-behave campaign status --campaign-id 1234567 --group-by scenario
uv run pro-behave campaign units --campaign-id 1234567 --state failed --format csv
uv run pro-behave campaign history --campaign-id 1234567 \
  --feature features/cli/attach.feature

# Follow it from a terminal, reconciling as it goes.
uv run pro-behave campaign events --campaign-id 1234567 --kinds actionable --follow

# Bundle a complete campaign's evidence.
uv run pro-behave campaign export --campaign-id 1234567

# A campaign directory anywhere else: its id is the directory's name.
uv run pro-behave campaign status --campaign /tmp/1234567
```

The same CLI runs one-off jobs (`pro-behave job …`), reads the feature
catalogue and sets the checkout's config; see the
[CLI reference](../../../../dev-docs/reference/pro_behave_cli.md).

All commands print JSON to stdout; `units` and `history` also take
`--format csv`, one row per unit (or per attempt, for `history`). `status`,
`units` and `history` accept the same filters plus `--state`, repeatable to
allow several values.

A campaign is named the way the server names it: `--campaign-id 1234567`
is `<repo>/.pro-behave/campaigns/1234567/` (or under
`$PRO_BEHAVE_STATE_DIR`), so the CLI and the MCP tools read the same record by the
same name. `--campaign DIR` addresses a campaign directory anywhere instead;
its id is always the directory's name. `--repo-root` defaults to `$UBUNTU_PRO_CLIENT_REPO`.

`events` reads the events the campaign's records announce; it needs no
server of its own. `--since-seq` and
`--kinds` work as they do for `await_campaign_events`. With `--follow` it
prints one batch per line as events arrive, polling every `--interval`
seconds, and returns once the campaign is paused or complete. Every command
ends with a reconcile, so running any one moves every campaign on the
checkout forward.

The CLI prints the same response shapes the MCP tools return. `create` and
`status` carry the campaign's header (how it was built: scope, checkout,
install source, max jobs) under `campaign` and its summary (state, intent,
progress, jobs in flight, counts by unit state) under `summary`. `run`,
`pause`, `retry` and `events` carry the summary fields at top level beside
their own; `list` gives each campaign's summary with the header fields that
identify it; `units`, `history` and `export` carry neither. `status` reports
counts, in-flight units and problems; `units` and `history` list individual
units, capped at `--limit` and saying so.

`create` records `--install-from` (default `local`) and max jobs for each
pool the campaign's units draw on:

| Pool | Machine types | Flag |
| --- | --- | --- |
| `lxd` | `lxd-container`, `lxd-vm` | `--max-lxd-jobs` |
| `cloud` | `aws.*`, `gcp.*`, `azure.*` | `--max-cloud-jobs` |
| `wsl` | `wsl` | `--max-wsl-jobs` |

A pool in scope that is not given gets 1. Max jobs for a pool no unit draws
on are rejected, because they usually mean the scope is not what was meant,
and `wsl` takes at most 1 because WSL jobs share one Windows host. Each pool
fills separately, so a busy WSL job never holds back LXD work. A campaign's
max jobs is its own share: the checkout's max jobs
(`pro-behave config`) still caps how many jobs run at once in all.

Splitting work across people happens out of band: each person creates their
own campaign with the slice they agreed to run.

## Guardrails

The tool fails loudly instead of guessing:

- A campaign can only be created once; it is not edited afterwards.
- A scope holding a unit that two scenarios of the same name both cover is
  rejected, naming the unit: behave would run both, so no job could name
  exactly one. Rename one of the scenarios, or narrow the scope.
- An unknown release, machine type, or feature in `create` is rejected rather
  than silently matching nothing.
- A record whose outcome, intent, cause or type is not a known value is
  rejected when the campaign is read.
- A record a campaign could not have written, such as an attempt on a
  campaign that never ran, is reported as inconsistent rather than read.

## MCP results

Reconcile records each finished job as the outcome of one attempt. Each
job covers exactly one unit, so the mapping is:

| MCP payload | Outcome |
| --- | --- |
| `finished`, every scenario passed | `passed` |
| `finished`, any scenario failed | `failed` |
| `finished`, only skips | `skipped` |
| `finished`, no parseable report (`summary: null`) | `error` |

An `interrupted` job, killed or dead, closes its attempt as `interrupted`
with that reason. It has no outcome, so the unit runs again while the
campaign runs.

Anything else -- a scenario status outside passed/failed/skipped, no scenario
counts at all, or a pass contradicted by `ok: false` -- cannot be classified.
A skipped Examples row alongside a passing one is not one of these: that run
passed. Reconcile records an unclassifiable result as `error`, keeps the
reason, and goes on rather than stopping the campaign.

## MCP tools

The server exposes campaigns through these tools. They read and write the
same files the CLI does, under the directory named by
`PRO_BEHAVE_STATE_DIR` (default `.pro-behave` in the server's checkout), in
its `campaigns/` subdirectory. Only `create_campaign` takes a `repo_root`: the
checkout whose feature files define the scope and whose jobs run.

- `create_campaign` -- plan a campaign and store it, starting nothing.
  Returns the unit count so scope can be confirmed first. `max_jobs` takes
  `lxd`, `cloud` and `wsl`; each may not exceed the checkout's max jobs for
  that pool (`pro-behave config`), and a campaign with units in a
  disabled pool is rejected. A campaign that would silently run
  slower than asked is rejected instead.
- `list_campaigns` -- every stored campaign: its state, the header fields
  that identify it, and how many values each scope filter names.
- `campaign_status` -- one campaign's counts, its jobs in flight in each pool,
  the units in flight, and the units needing action, one unit per row or -- with `group_by=scenario` --
  one scenario per row with its units bucketed by state, which is how a
  scenario failing on every release reads at a glance. Either list is capped
  at `problems_limit` (50 over MCP; 2000 from the CLI) with `problems_total`
  alongside. Individual units are `unit_history`'s job.
- `control_campaign` -- `run` or `pause`, from any state. `run` sets the
  intent to run and queues what the campaign's max jobs allow at once; the
  server's reconcile, every 5 seconds and after every tool call, records
  each outcome and queues the next unit, so no further calls are needed to
  keep it moving.
  `pause` sets the intent to hold; with `kill: true` it also kills the jobs
  in flight. See [States](#states).
- `await_campaign_events` -- wait for news, with a cursor.
- `unit_history` -- every attempt at each selected unit: job, install
  source, timestamps and outcome. How a unit got to where it is, without
  reading the event stream.
- `retry_units` -- ask for another attempt at units that already had one,
  and set the intent to run.
- `kill_job` -- kill a job that has hung.
- `export_campaign` -- write a complete campaign's evidence bundle, the
  same as `pro-behave campaign export`.

## Events

A campaign announces what happens to it. The record is still the truth; this
is how a watcher hears about it promptly.

| Family | Kinds |
| --- | --- |
| `campaign.*` | `created`, `running`, `finishing`, `complete`, `pausing`, `paused` |
| `job.*` | `started`, `overdue` |
| `unit.*` | `passed`, `failed`, `skipped`, `errored`, `unclassifiable`, `interrupted`, `retried` |
| `anomaly.*` | `repeated_scenario_failure`, `repeated_skips`, `starved` |

A `campaign.<state>` event is emitted each time the campaign's named state
changes, and only then. Its data says the state it came `from`, the `intent`
and `progress` now, the `cause` (`run`, `pause`, `retry` or `scheduler`) and
the `counts` by unit state.

Subscribe by exact kind, by family (`unit.*`), or by preset: `actionable`
is every non-passing outcome, `anomaly.*`, `job.overdue` and `campaign.*`
-- what a watcher has to react to, with progress left to the counts on
every response -- and `*` is everything. The MCP tool defaults to
`actionable`; the CLI to everything. An unknown kind or family is rejected
rather than quietly matching nothing, because a typo would otherwise look
like a campaign that never emits.

A `unit.failed` event carries the failing steps and their messages, so a
failure can be judged without fetching the job's report; `failure_chars`
shortens the messages on read for a caller that only wants to skim. A
`unit.unclassifiable` event carries the reason the result could not be read.

Events are not stored. Each read derives them from the campaign's records,
one pass over the log: a record announces what it says (`job.started`,
`unit.failed`, `unit.retried`, `anomaly.starved`, `job.overdue` and
so on), and any change of the campaign's state it causes. `seq` is an event's
position in that feed, so it is dense, and appending records never renumbers
an earlier event: a cursor survives a restart, and two readers in different
processes see the same numbers. Pass `next_seq` from one response as the next
`since_seq`. A reader waiting with a timeout is woken by the server's own
appends at once, and notices another process's within a second.

`anomaly.*` and `job.overdue` are derived: nothing acts on them. A run of
skips usually means a config the host does not have, and one scenario failing
across every release usually means a real defect rather than flake -- but
which of those it is, and what to do about it, is not for an unattended loop
to decide. Each is announced once per condition, so a job that stays slow
does not flood the stream: a repeated failure or run of skips the first time
it holds, a starved pool once until a job is next queued there, an overdue
job once, timed from when the job started running rather than from when it
was queued. Starvation and overdue are written as records, so a restart does not
announce them again.

There is deliberately no heartbeat. Every response carries the campaign's
counts and state, so a batch that came back empty on timeout still says
where things stand, and an agent that has stopped polling would not receive a
heartbeat anyway.

## States

A campaign's state has three parts. Only the first is written:

| Part | Values | Comes from |
| --- | --- | --- |
| intent | `never_run`, `run`, `hold` | the latest `intent` record; `never_run` is its absence |
| progress | `untouched`, `open`, `finishing`, `settled` | the units |
| counts | units per state | the units |

Progress is `untouched` while no unit has an attempt, `open` while any unit
is queueable, `finishing` while nothing is queueable but a job is in flight,
and `settled` otherwise. The named state is read off intent, progress and
whether any job is in flight:

| State | Intent | Progress | What reconcile does |
| --- | --- | --- | --- |
| `created` | never run | untouched | nothing |
| `running` | run | untouched or open | records ended jobs, queues queueable units up to max jobs |
| `finishing` | run | finishing | records ended jobs |
| `complete` | run or hold | settled | nothing |
| `pausing` | hold | open or finishing, with a job in flight | records ended jobs, queues nothing |
| `paused` | hold | untouched or open, nothing in flight | nothing |

A unit is in flight from the moment its job is queued, including while it
waits for a lane. An interrupted job, killed or dead, makes its unit
queueable, so under intent run reconcile queues it again as a new attempt;
under hold it waits for the next `run`.

The commands:

- `run` sets the intent to run, from any state, and reconciles. On a
  complete campaign it changes nothing: there is no work to queue.
- `pause` sets the intent to hold, from any state. Jobs in flight finish and
  are recorded, so the campaign is `pausing` until they do. With `kill`, each
  job in flight is killed and its attempt is recorded as `interrupted` at
  once, so the campaign is paused at once and those units run again on the
  next `run`. The response lists those units in `killed`, and names a job
  whose process cannot be reached in `not_killed`.
- `retry_units` requests another attempt at the selected units, sets the
  intent to run, and reconciles, in any state.

`complete` is not something anyone writes or asks for. It is when the
evidence is final, so it gates export, and when a watcher can stop. It is not
terminal: `retry_units` on a complete campaign gives it work and sets it
running.

Reconcile never retries. A unit that failed, was skipped, or errored stays
that way until someone asks for another attempt, because judging a failure
flaky-or-real is not something an unattended loop should decide. A `retry`
record makes the unit queueable until its next attempt starts; the file still
says who asked and when.

Any number of campaigns may run at once, alongside one-off jobs. Nothing
long-lived drives them: `CampaignService.reconcile` brings every campaign up
to date and then starts queued jobs, and it runs after every MCP tool call,
every 5 seconds in the server, and after every CLI command. A job's own
process starts the next queued job when it ends, but it knows nothing of
campaigns. Reconcile decides from files alone, under the state
directory's one `reconcile.lock`, which `run`, `pause`, `retry_units` and
`create` take too. A restart therefore changes nothing: a new process reads
the same files and carries on.

## Types

Every set of related-but-mutually-exclusive values is an enum, so an
exhaustive check is possible and a typo is a failure rather than a value
that silently matches nothing:

| enum | values | module |
| --- | --- | --- |
| `RecordType` | the nine record types above | `domain/records.py` |
| `Outcome` | `passed`, `failed`, `skipped`, `error` | `domain/records.py` |
| `UnitState` | those four, plus `unattempted` and `in_flight` | `domain/records.py` |
| `Intent` | `never_run`, `run`, `hold` | `domain/records.py` |
| `Progress` | `untouched`, `open`, `finishing`, `settled` | `domain/records.py` |
| `CampaignState` | `created`, `running`, `finishing`, `complete`, `pausing`, `paused` | `domain/records.py` |
| `GroupBy` | `unit`, `scenario` | `domain/state.py` |
| `ControlAction` | `run`, `pause` | `domain/schedule.py` |
| `EventKind` | every event, with a `family` property | `domain/events.py` |
| `Cause` | `run`, `pause`, `retry`, `scheduler` | `domain/records.py` |
| `EventFamily` | `campaign`, `job`, `unit`, `anomaly` | `domain/events.py` |

They all subclass `StringEnum`, so a member reads and serialises as its
value (`"hold"`, not `"Intent.HOLD"`) and compares equal to the plain
string a caller sent. Plain-string tuples are derived from each (`OUTCOMES`,
`STATES` and so on) for argparse choices and error messages.

## Architecture

`pro_behave.campaign` uses the same hexagonal layering as
[`pro_behave.jobs`](../jobs). The pure layers are packages with one module
per concern.

`domain/` holds the campaign rules. No module in it does I/O:

- `records.py`: the record types, enums and filters, and how records are
  read and written.
- `state.py`: what the records say now: unit states, intent, progress, the
  named state, problems.
- `schedule.py`: what one reconcile decides for a campaign.
  `plan_campaign` chooses which ended jobs to record and which units to
  queue.
- `classify.py`: what a finished job established for its unit.
- `events.py`: event kinds, subscription presets and matching, and the
  event a change of state announces.
- `signals.py`: the thresholds for derived signals, and which jobs are
  overdue.
- `feed.py`: derives every event from the record log.
- `discovery.py`: builds units from the feature files through
  `pro_behave.jobs.parser`.
- `bundle.py`: what a complete campaign's evidence bundle holds.

`views/` turns records into what a caller or reviewer reads, and decides
nothing:

- `responses.py`: builds the response DTOs defined in `messages.py`.
- `export.py`: the bundle's `SUMMARY.md`, `export.json` and `units.csv`.

The other modules:

- `messages.py`: pydantic DTOs returned across the MCP boundary.
- `ports.py`: the Protocols the service depends on: `CampaignStore`,
  `FeatureReader`, `JobRunner` (the job service, as a campaign uses it),
  `JobFiles`, `BundleWriter`.
- `adapters.py`: concrete implementations. Two stores satisfy
  `CampaignStore`: the MCP names a campaign by id under the campaigns
  directory, and the CLI can also be pointed at one campaign's directory.
  Both share one serialisation path, so there is one on-disk format.
  `pro_behave.jobs.domain.layout` names the directory convention.
- `repo.py`: reads the checkout state a campaign was built from.
- `service.py`: `CampaignService`, built per call by the MCP server and the
  CLI (a job process builds only the job service). It creates and reports on
  campaigns, runs the commands, and reconciles. Behaviour changes belong
  here; its decisions are `schedule.plan_campaign`'s, so tests drive it by
  calling `reconcile` themselves.

## Build, test, lint

Run from `tools/pro-behave`; the campaign part shares that project's
`pyproject.toml` and virtualenv.

```bash
uv sync --extra test --extra lint
uv run pytest -q tests/campaign
uv run black --check pro_behave/campaign
uv run isort --check-only pro_behave/campaign
uv run flake8 pro_behave/campaign
uv run mypy pro_behave/campaign
uv run lint-imports
```

## TODOs

- Consider stronger DB than JSONL with various record types

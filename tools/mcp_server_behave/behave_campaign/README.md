# behave_campaign

Durable record of behave test units and attempts.

A campaign outlives the MCP's job history. The MCP's `list_scenario_jobs`
window is bounded; an SRU verification runs for days, so what was attempted
and what it established is recorded here instead.

A **test unit** is one behave scenario for one release on one `machine_type`.

## Storage

One append-only JSON Lines file per campaign, replayed in full to get the
current state. Six record types:

| type | written when | says |
| --- | --- | --- |
| `campaign` | once, first line | how the campaign was built: scope, checkout, install source, lanes |
| `plan` | once per unit, at creation | this unit is in scope |
| `started` | a lane opens | a job began for this unit, and what it installs from |
| `finished` | that job ends | what it established: `passed`, `failed`, `skipped` or `error` |
| `lifecycle` | a control verb | the campaign was started, paused, cancelled or reopened, and why |
| `retry` | `retry_units` | someone asked for another go at a unit |

One try at a unit is one job, written as two records. Both are needed: the
`started` record is what holds the lane, including across a restart. Neither
is an attempt on its own -- an **attempt** is the pair, matched on `job_id`,
and that is what `attempts` and `attempt_count` report. A unit tried twice has
two attempts, not four.

`running` is therefore not an outcome. A unit is running when its latest
attempt has no `finished` record yet, which is a fact about the attempt rather
than a result the job reported.

The `campaign` header captures how the campaign was built, so it can be
recreated and audited later: the campaign id, the filters used, the checkout it
was built from (root, commit, branch, dirty), and when. The header also records the
`install_from` every job runs with and the `max_lanes` a runner may fill; both
are required, so a campaign always says how it is meant to be run. Every record carries a
UTC timestamp, and every attempt records the install source the job ran with --
the evidence an SRU verification actually rests on, which would otherwise only
live in MCP job history that expires.

A unit's state is derived, never stored:

- No attempts at all is `unattempted`.
- Any passing attempt makes and keeps the unit `passed`.
- An unfinished latest attempt is `running`.
- Otherwise the latest attempt's outcome wins: `failed`, `skipped` or
  `error`.

## Scope

`create` builds units from the feature files themselves, parsed with
`behave_mcp.parser` -- the same parser the MCP uses to select scenarios at run
time, so a campaign covers exactly the combinations each scenario supports
rather than a Cartesian product.

`create` defines scope once, from repeatable `--release`, `--machine-type`,
`--feature`, and `--scenario` filters; omitting them all yields the full
campaign. `--campaign-id` is an opaque label -- for SRU work it is the
Launchpad bug number, but the record attaches no meaning to it.

## Usage

```bash
uv sync --extra test

# See which releases and machine types the feature files can run.
uv run behave-campaign dimensions --repo-root ../..

# Create the campaign: "all the jammy lxd-vm tests".
uv run behave-campaign create --campaign T.jsonl --repo-root ../.. \
  --campaign-id 1234567 --release jammy --machine-type lxd-vm

# Repeat a filter to cover several values, as a real SRU usually does.
uv run behave-campaign create --campaign T.jsonl --repo-root ../.. \
  --campaign-id 1234567 \
  --release bionic --release focal --release jammy \
  --release noble --release resolute --release stonking

# With no filters, the campaign covers every feature file.
uv run behave-campaign create --campaign T.jsonl --repo-root ../.. \
  --campaign-id 1234567

# Record the install source and lane count for a runner to honour.
uv run behave-campaign create --campaign T.jsonl --repo-root ../.. \
  --campaign-id 1234567 --install-from proposed --max-lanes 8

# Record started and finished attempts in batches.
uv run behave-campaign record --campaign T.jsonl --install-from proposed \
  --input attempts.json

# Or hand the MCP's own start/wait payloads straight to the tool.
uv run behave-campaign record --campaign T.jsonl --install-from proposed \
  --from-mcp --input results.json

# Ask what to run next, then inspect results.
uv run behave-campaign next --campaign T.jsonl --limit 4
uv run behave-campaign status --campaign T.jsonl --state failed
uv run behave-campaign status --campaign T.jsonl --group-by scenario
uv run behave-campaign status --campaign T.jsonl --include-units --limit 50
uv run behave-campaign history --campaign T.jsonl \
  --feature features/cli/attach.feature
```

# Follow a campaign a server is running, from a terminal.
uv run behave-campaign events --campaign T.jsonl --kinds 'unit.*' --follow

All commands read `--input` from stdin by default and print JSON to stdout.
`status`, `next`, and `history` accept the same filters plus `--state`,
repeatable to allow several values.

`events` reads the log a server writes beside the campaign file
(`T.events.jsonl`); it needs no server of its own. `--since-seq` and
`--kinds` work as they do for `await_campaign_events`. With `--follow` it
prints one batch per line as events arrive, polling every `--interval`
seconds, and returns once the campaign is complete or cancelled with no
lane in flight. Control verbs have no CLI form: the scheduler and its run
lock live in the server process.

The CLI prints the same response shapes the MCP tools return. `create` and
`status` carry the campaign's header (how it was built: scope, checkout,
install source, lanes) under `campaign` and its state (lifecycle, lanes in
flight, counts by unit state) under `state`; every other response carries
the state fields alone, since the header never changes. `status` and
`history` omit or cap large unit lists the same way: `status` reports counts, in-flight units
and problems, and lists every selected unit only with `--include-units`.

`create` optionally records `--install-from` and `--max-lanes` for a runner to
honour later; both are omitted from the file when not given. `--campaign-id`
defaults to the campaign file's name.

Splitting work across people happens out of band: each person creates their
own campaign file with the slice they agreed to run.

## Guardrails

The tool fails loudly instead of guessing:

- A campaign file can only be initialised once; a campaign is not edited
  afterwards.
- An unknown release, machine type, or feature in `create` is rejected rather
  than silently matching nothing.
- Recording an attempt for a unit that was never planned is rejected.
- Attempt state must be one of the known states.
- An MCP result that is not a clean pass, failure, or skip is rejected.

## MCP results

`record` takes completed tries: `[{unit..., "job_id": ..., "outcome": ...}]`,
and writes both halves for each, since a caller recording out of band is
describing a whole attempt. `--from-mcp` takes
`[{"unit": {...}, "result": <MCP payload>}]` instead and reads the outcome out
of the payload. Each job covers exactly one unit, so the mapping is:

| MCP payload | Outcome |
| --- | --- |
| `completed`, every scenario passed | `passed` |
| `completed`, any scenario failed | `failed` |
| `completed`, only skips | `skipped` |
| `completed`, no parseable report (`summary: null`) | `error` |

A job still in flight cannot be recorded this way. The scheduler owns those:
it wrote the `started` record itself and will write the finish.

Anything else -- a scenario status outside passed/failed/skipped, no scenario
counts at all, or a pass contradicted by `ok: false` -- cannot be classified.
A skipped Examples row alongside a passing one is not one of these: that run
passed.

The CLI rejects an unclassifiable result so it gets looked at rather than
silently recorded. A scheduler has nobody to raise at, so it records the
attempt as `error` and keeps the reason, rather than stopping the campaign.

## MCP tools

The server exposes the campaign through three tools so far. They read and
write the same files the CLI does, under the directory named by
`MCP_STATE_DIR` (default `<repo_root>/.mcp_server_behave`), in its
`campaigns/` subdirectory.

- `create_campaign` -- plan a campaign and store it, starting nothing.
  Returns the unit count so scope can be confirmed first. `max_lanes` may
  not exceed `MCP_MAX_PARALLEL_JOBS`; a campaign that would silently run
  serially is rejected instead.
- `list_campaigns` -- every stored campaign: its state, the header fields
  that identify it, and how many values each scope filter names.
- `campaign_status` -- one campaign's counts, the units in flight, and the
  units needing action, one unit per row or -- with `group_by=scenario` --
  one scenario per row with its units bucketed by state, which is how a
  scenario failing on every release reads at a glance. Either list is capped
  at `problems_limit` (50 over MCP; the CLI lists all) with `problems_total`
  alongside. Individual units are opt-in via `units_limit` because a full
  campaign is over a thousand of them.
- `start_campaign` -- begin scheduling. The server then keeps up to
  `max_lanes` jobs in flight and fills a lane as soon as one frees, with no
  further calls needed to keep it moving.
- `pause_campaign` / `resume_campaign` -- stop and restart lane opening.
- `cancel_campaign` -- close a campaign to further scheduling, including
  one that has already finished.
- `reopen_campaign` -- take a cancellation back, for one made by mistake.
- `await_campaign_events` -- wait for news, with a cursor.
- `unit_history` -- every attempt at each selected unit: job, install
  source, timestamps and outcome. How a unit got to where it is, without
  reading the event stream.
- `retry_units` -- ask for another attempt at units that already had one.
- `kill_job` -- terminate a job that has hung.

## Events

A campaign announces what happens to it. The record is still the truth; this
is how a watcher hears about it promptly.

| Family | Kinds |
| --- | --- |
| `campaign.*` | `created`, `started`, `paused`, `resumed`, `cancelled`, `reopened`, `complete` |
| `lane.*` | `started`, `overdue` |
| `unit.*` | `passed`, `failed`, `skipped`, `errored`, `unclassifiable`, `retried` |
| `anomaly.*` | `repeated_scenario_failure`, `repeated_skips`, `capacity_starved` |

Subscribe by exact kind, by family (`unit.*`), or by preset: `actionable`
is every non-passing outcome, `anomaly.*`, `lane.overdue` and `campaign.*`
-- what a watcher has to react to, with progress left to the counts on
every response -- and `*` is everything. The MCP tool defaults to
`actionable`; the CLI to everything. An unknown kind or family is rejected
rather than quietly matching nothing, because a typo would otherwise look
like a campaign that never emits.

A `unit.failed` event carries the failing steps and their messages, so a
failure can be judged without fetching the job's report. A
`unit.unclassifiable` event carries the reason the result could not be read.

`seq` is dense and monotonic within a campaign, so a cursor never skips: pass
`next_seq` from one response as the next `since_seq`. Events are appended to
`<campaign_id>.events.jsonl` and held in memory for serving, which is what
lets a cursor survive a restart and a blocked reader be woken by an append.

`anomaly.*` and `lane.overdue` are derived: nothing acts on them. A run of
skips usually means a config the host does not have, and one scenario failing
across every release usually means a real defect rather than flake -- but
which of those it is, and what to do about it, is not for an unattended loop
to decide. Each is reported once per condition rather than every tick, keyed
on the condition itself, so a lane that stays slow does not flood the stream.

There is deliberately no heartbeat. Every response carries the campaign's
counts and lifecycle, so a batch that came back empty on timeout still says
where things stand, and an agent that has stopped polling would not receive a
heartbeat anyway.

## Lifecycle

A campaign is `created` until it is started, then `running`, and `complete`
once nothing is unattempted or in flight. `paused` and `cancelled` are asked
for; `complete` is derived from the counts, so it can never disagree with the
units.

The latest `lifecycle` record is the current state, so a state is a position
rather than a door that locks behind you. `reopen_campaign` uses that: it
appends a `running` record after a `cancelled` one, leaving both in the file,
so a campaign closed by mistake reads as one that was closed and reopened.

Both `pause` and `cancel` **drain**: they stop opening lanes, but jobs already
in flight run to completion and their results are still recorded. `lanes_busy`
in the response says how many are still draining. Nothing kills a job.

The scheduler never retries. A unit that failed, was skipped, or errored stays
that way until someone asks for another attempt, because judging a failure
flaky-or-real is not something an unattended loop should decide.

`retry_units` is how that ask is made. It appends a `retry` record, so a unit
becomes schedulable again when it carries one newer than its last attempt --
the file still says who asked and when, and running it appends a `running`
attempt that clears the request. Retrying a campaign that had finished starts
it scheduling again; a paused one accepts the request and stays paused.

Cancelling is what closes a campaign against being picked up this way, which
is the only thing `cancel` does that `pause` cannot -- a finished campaign
cannot be paused. So cancel a run to say it is over, not to stop it for now.
A campaign cancelled by mistake is not lost: `reopen_campaign` puts it back
where it stood. One with units still unattempted starts scheduling again; one
whose units were all attempted comes back `complete`, and `retry_units` then
reaches its failed and skipped units as before.

Reopening does not recover jobs, only the campaign. A cancel that left lanes
in flight and then outlived its server leaves units `running` against jobs
nothing will ever report on, and `reopen_campaign` refuses while it can see
them rather than scheduling around units that can never resolve. Naming
`abandon_in_flight` records them as `error`, with `abandoned_on_reopen` on
the `unit.errored` event, which makes them retryable like any other
failure.

Lane state lives in the record, not in memory: a unit in flight is one whose
latest attempt is `running`, written before the lane is released. That is what
lets a tick pick up where the last one left off, and why a campaign left
running by a server that went away can be recovered at all -- it comes back
`paused`, with `server_restart` as the reason, so whoever is watching decides
whether those jobs are still alive.

## Types

Every set of related-but-mutually-exclusive values is an enum in `domain.py`,
so an exhaustive check is possible and a typo is a failure rather than a value
that silently matches nothing:

| enum | values |
| --- | --- |
| `RecordType` | the six record types above |
| `Outcome` | `passed`, `failed`, `skipped`, `error` |
| `UnitState` | those four, plus `unattempted` and `running` |
| `Lifecycle` | `created`, `running`, `paused`, `cancelled`, `complete` |
| `EventKind` | every event, with a `family` property |
| `EventFamily` | `campaign`, `lane`, `unit`, `anomaly` |

They all subclass a small `_StringEnum`, so a member reads and serialises as
its value -- `"paused"`, not `"Lifecycle.PAUSED"` -- and compares equal to the
plain string a caller sent. Plain-string tuples are derived from each
(`OUTCOMES`, `STATES`, ...) for argparse choices and error messages.

## Architecture

Same hexagonal layering as [behave_mcp](../behave_mcp), one module per layer:

- `domain.py` -- pure campaign rules. Units, attempts, state reduction, and
  the order remaining work is taken up in. No I/O.
- `ports.py` -- the Protocols the service and runner depend on:
  `CampaignStore`, `FeatureReader`, `LaneRunner`, `EventLog`, `Ticker`,
  `CampaignRunLock`.
- `adapters.py` -- concrete implementations. Two stores satisfy
  `CampaignStore` because the front-ends address campaigns differently: the
  MCP names one by id inside a campaign directory, the CLI is pointed at a
  file. Both share one serialisation path, so there is one on-disk format.
- `service.py` -- `CampaignService`, driven by both the MCP tool wrappers and
  the CLI. Where behaviour changes belong.
- `messages.py` -- pydantic DTOs returned across the MCP boundary.
- `discovery.py` -- builds units from the feature files via
  `behave_mcp.parser`.
- `repo.py` -- reads the checkout state a campaign was built from.
- `runner.py` -- `CampaignRunner`, which opens lanes, records what finishes,
  and holds the run lock for the one campaign that may be active. The only
  module here that starts a thread, and it holds no scheduling decisions:
  those are `domain.plan_tick`, so the scheduler is testable without one.
  `ThreadTicker` is the `Ticker` port's real implementation; tests inject a
  fake and call `tick` themselves.
- `cli.py` -- the standalone front-end. Wrappers only: read arguments, call
  the service, print the response as JSON. Behaviour belongs in `service.py`
  so the CLI and the MCP tools cannot drift apart.

The run lock is an advisory `flock` on `<campaign_id>.lock`, held for as long
as a campaign is actively scheduling, so a concurrent CLI fails cleanly
instead of interleaving writes. The kernel drops it if the holder dies.

## Build, test, lint

Run from `tools/mcp_server_behave`; this package shares that project's
`pyproject.toml`, virtualenv, and CI job.

```bash
uv sync --extra test           # or --extra lint
uv run pytest -q tests/behave_campaign
uv run black --check behave_campaign
uv run isort --check-only behave_campaign
uv run flake8 behave_campaign
uv run mypy behave_campaign
```

## TODOs

- Consider stronger DB than JSONL with various record types
- Find common data models with the MCP; avoid duplicate serde if we can.
  `repo.py` and `behave_mcp.adapters.LocalWorkspace.repo_state` read the same
  git state through different types.

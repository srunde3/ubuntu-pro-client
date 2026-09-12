# behave_campaign

Durable record of behave test units and attempts.

A campaign outlives the MCP's job history. The MCP's `list_scenario_jobs`
window is bounded; an SRU verification runs for days, so what was attempted
and what it established is recorded here instead.

A **test unit** is one behave scenario for one release on one `machine_type`.

## Storage

One append-only JSON Lines file per campaign. `create` writes a `campaign`
header first, then one `plan` record per unit; `record` appends `attempt`
records. The whole campaign replays from the file.

The `campaign` header captures how the campaign was built, so it can be
recreated and audited later: the campaign id, the filters used, the checkout it
was built from (root, commit, branch, dirty), and when. The header also records the
`install_from` every job runs with and the `max_lanes` a runner may fill; both
are required, so a campaign always says how it is meant to be run. Every record carries a
UTC timestamp, and every attempt records the install source the job ran with --
the evidence an SRU verification actually rests on, which would otherwise only
live in MCP job history that expires.

Current state per unit:

- A unit with no attempts is `unattempted`.
- Any passing attempt makes and keeps the unit `passed`.
- Otherwise the latest attempt state wins: `running`, `failed`, `skipped`,
  or `error`.

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
uv run behave-campaign status --campaign T.jsonl --include-units --limit 50
uv run behave-campaign history --campaign T.jsonl \
  --feature features/cli/attach.feature
```

All commands read `--input` from stdin by default and print JSON to stdout.
`status`, `next`, and `history` accept the same filters plus `--state`,
repeatable to allow several values.

The CLI prints the same response shapes the MCP tools return, so counts sit
under `campaign` alongside the header fields. `status` and `history` omit or
cap large unit lists the same way: `status` reports counts, in-flight units
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

`record --from-mcp` takes `[{"unit": {...}, "result": <MCP payload>}]`, where
`result` is a `start_scenario` or `wait_for_scenario_completion` response. Each
job covers exactly one unit, so the mapping is:

| MCP payload | State |
| --- | --- |
| `started`, `timeout` | `running` |
| `completed`, every scenario passed | `passed` |
| `completed`, any scenario failed | `failed` |
| `completed`, only skips | `skipped` |
| `completed`, no parseable report (`summary: null`) | `error` |

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
`MCP_CAMPAIGN_DIR` (default `<repo_root>/.mcp_server_behave/campaigns`).

- `create_campaign` -- plan a campaign and store it, starting nothing.
  Returns the unit count so scope can be confirmed first. `max_lanes` may
  not exceed `MCP_MAX_PARALLEL_JOBS`; a campaign that would silently run
  serially is rejected instead.
- `list_campaigns` -- every stored campaign with its counts by state.
- `campaign_status` -- one campaign's counts, the units in flight, and the
  units needing action. Individual units are opt-in via `units_limit`
  because a full campaign is over a thousand of them.
- `start_campaign` -- begin scheduling. The server then keeps up to
  `max_lanes` jobs in flight and fills a lane as soon as one frees, with no
  further calls needed to keep it moving.
- `pause_campaign` / `resume_campaign` -- stop and restart lane opening.
- `cancel_campaign` -- close a campaign to further scheduling, for good.
- `await_campaign_events` -- wait for news, with a cursor.
- `retry_units` -- ask for another attempt at units that already had one.
- `kill_job` -- terminate a job that has hung.

## Events

A campaign announces what happens to it. The record is still the truth; this
is how a watcher hears about it promptly.

| Family | Kinds |
| --- | --- |
| `campaign.*` | `created`, `started`, `paused`, `resumed`, `cancelled`, `complete` |
| `lane.*` | `started`, `released`, `overdue` |
| `unit.*` | `passed`, `failed`, `skipped`, `errored`, `unclassifiable`, `retried` |
| `anomaly.*` | `repeated_scenario_failure`, `repeated_skips`, `capacity_starved` |

Subscribe by exact kind or by family (`unit.*`); omit `kinds` for everything.
An unknown kind or family is rejected rather than quietly matching nothing,
because a typo would otherwise look like a campaign that never emits.

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
Cancelling is what closes a campaign against being reopened this way.

Lane state lives in the record, not in memory: a unit in flight is one whose
latest attempt is `running`, written before the lane is released. That is what
lets a tick pick up where the last one left off, and why a campaign left
running by a server that went away can be recovered at all -- it comes back
`paused`, with `server_restart` as the reason, so whoever is watching decides
whether those jobs are still alive.

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

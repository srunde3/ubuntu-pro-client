---
name: run-sru-test-suite
description: 'Run and track the Ubuntu Pro Client Behave test suite across releases and machine types for an SRU. Use when verifying the features/ integration suite for an SRU, or resuming an SRU verification already under way.'
argument-hint: 'Describe the SRU verification run'
---

# Run SRU Test Suite

## Scope

SRU verification of the Behave integration tests in `features/`, using the
`behave` MCP server's campaign tools. For running one scenario ad hoc, use
[feature-test-runs](../feature-test-runs/SKILL.md) instead.

## The split

A **campaign** is the durable record of an SRU verification: which test units
are in scope, what has been attempted, and what each attempt established. A
**unit** is one scenario for one release on one `machine_type`. An **attempt**
is one try at a unit, which is one behave job.

The server runs the campaign. It keeps up to `max_lanes` jobs in flight, fills
a lane the moment one frees, classifies every result, and records everything.
You do not start jobs, translate results, or track progress.

You make the decisions it deliberately does not:

- what is in scope, agreed with the user
- whether a failure is real or flaky, and what deserves another attempt
- whether the run is worth continuing at all

Read each tool's own description for its parameters and response shape. Do not
rely on this file for those; it will drift and the tool will not.

## Define scope

1. Ask for a Launchpad SRU bug URL or numeric ID, and normalise a URL to the
	ID. It identifies the campaign locally; do not read or update Launchpad.
2. Call `list_dimensions` and show the user those exact release and
	`machine_type` values. Do not translate aliases or invent values.
3. Ask whether this is a full run or a named set of feature files or
	scenarios.
4. Call `create_campaign` with the agreed scope, `install_from: proposed`, and
	a `max_lanes` the host can stand. Nothing runs yet.
5. Report the unit count it returns and **get the user's confirmation before
	calling `start_campaign`**.

If `create_campaign` rejects the scope -- an unknown release, a `max_lanes`
above the server's job limit, an id already in use -- that is a scope mistake
to resolve with the user, not an error to work around.

## Run it

Call `start_campaign`, then follow the campaign with `await_campaign_events`,
passing back the `next_seq` it returns so the stream has no gaps. Between
batches you have nothing to do: lanes refill without you.

Every response also carries the campaign's counts and lifecycle, so an empty
batch still tells you where things stand. There is no heartbeat to wait for.

Subscribe to what you will act on. `unit.*` and `anomaly.*` is usually right;
add `campaign.*` to notice a pause you did not ask for.

## Judgement

- `unit.failed` carries the failing steps and their messages. Judge from
  those. Only fetch `get_scenario_logs` or `get_scenario_artifacts` if they
  are not enough.
- `unit.skipped` means configuration the host does not have. Report the
  missing variable names. Never ask the user for a secret value in chat.
- `unit.unclassifiable` is a finding to report, not an error to work around:
  the job produced something the classifier could not read.
- `anomaly.*` events are offered for judgement and nothing acts on them.
  Repeated skips usually mean missing config. One scenario failing across
  every release usually means a real defect rather than flake.
- `lane.overdue` means a job has run far longer than expected. Decide whether
  to `kill_job` it; the campaign then records that unit and frees the lane.

Let plain failures accumulate and keep going. The point of a first pass is a
complete picture, not a green one.

## Stop, retry, resume

`pause_campaign` and `cancel_campaign` both drain: jobs already running finish
and are recorded. Neither kills anything. `lanes_busy` in the response says how
many are still draining. Cancel when the run is not worth continuing -- a
broken checkout, infrastructure that will not recover -- and say why.

Nothing re-runs a non-passing unit on its own. `retry_units` is the only way,
and it selects failed, skipped and errored units unless you name a state.
Retrying a campaign that had finished starts it scheduling again; a paused one
accepts the request and stays paused.

Resuming: `list_campaigns` finds the campaign, `campaign_status` shows where it
got to. A campaign the server was running when it last stopped comes back
`paused`, with `server_restart` as the reason -- check its in-flight units
before calling `resume_campaign`.

## Finish

A campaign is `complete` when nothing is unattempted or in flight. Report
without giving an overall verdict:

- counts by state
- the failed, skipped and errored units with their `job_id`s
- the campaign id

## TODOs

If you are an agent, do not consider this section.

- Once the MCP exposes a config endpoint, say how to check for a contract
  token and cloud credentials up front instead of inferring from skips.

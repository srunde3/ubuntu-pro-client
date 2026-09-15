---
name: run-sru-test-suite
description: 'Run and track the Ubuntu Pro Client Behave test suite across releases and machine types for an SRU. Use when verifying the features/ integration suite for an SRU, or resuming an SRU verification already under way.'
argument-hint: 'Describe the SRU verification run'
---

# Run SRU Test Suite

## Scope

SRU verification of the Behave integration tests in `features/`, using the
`behave` MCP server's campaign tools.

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

Call `start_campaign`, then immediately delegate campaign monitoring to a
subagent (`runSubagent`). The human should not be required to check in or prompt
repeatedly; an extended monitoring subagent watches the event stream, triages
failures, and reports back.

### Subagent Delegation

Dispatch a subagent with a prompt instructing it to:
1. Long-poll `await_campaign_events` in a loop using `timeout_seconds: 60` and
   passing back `next_seq` on every cycle.
2. Filter strictly to actionable event kinds:
   ```json
   {
     "kinds": [
       "unit.failed",
       "unit.errored",
       "unit.unclassifiable",
       "anomaly.*",
       "lane.overdue",
       "campaign.*"
     ]
   }
   ```
   Do NOT subscribe to `unit.*` or leave `kinds` empty—pass events and lane churn
   bloat context. Aggregate counts (`passed`, `failed`, `running`, `unattempted`)
   and `lanes_busy` are already present in every response.
3. Triage failures directly from `data.failures` inline without reading logs
   unless `failures` is empty (indicating a harness/hook crash).
4. Run for an extended window (e.g. 10–15 polling cycles) or stop early if:
   - The campaign finishes (`running == 0` and `unattempted == 0`), OR
   - A systemic defect appears (host harness crash, environment misconfiguration,
     repeated hook failure across all lanes), OR
   - A lane is overdue and needs a kill/drain decision.
5. Return a concise, structured report back to the main agent:
   - Current campaign counts (`passed`, `failed`, `error`, `unattempted`, `running`)
   - Triaged failure summary (harness/flake/bug)
   - Specific decision points requiring human input, if any

When the subagent returns:
- If the campaign finished, proceed to `## Finish`.
- If an action was needed (e.g. host fix, retry), take the action with user confirmation.
- If the campaign is still healthy and running, report the progress update to the user and dispatch the next extended monitoring subagent.

## Judgement

### Failure Triage (Zero-Log Overhead)

- `unit.failed` carries `data.failures` with the failing `step` and
  `error_message`. Judge directly from those step assertions without calling
  `get_scenario_logs`.
- Only fetch `get_scenario_logs` if `data.failures` is empty (typically
  indicates a Behave hook error such as `after_step` or a test harness crash).
- Classify failures promptly:
  1. **Host/Harness Defect** (e.g. hook crash, file encoding, permission
     denial): Actionable immediately. Fix the harness or host environment, then
     use `retry_units` to re-queue affected units.
  2. **External Flake** (e.g. HTTP 503 from backend/CVE endpoints, network
     timeout): Note the transient failure; let it accumulate until the pass
     completes, then retry in batch.
  3. **Genuine Code Bug** (client regression or release incompatibility):
     Record it for the SRU record. Do not retry real defects.
- `unit.skipped` means configuration the host does not have. Report the
  missing variable names. Never ask the user for a secret value in chat.
- `unit.unclassifiable` is a finding to report, not an error to work around:
  the job produced something the classifier could not read.
- `anomaly.*` events are offered for judgement and nothing acts on them:
  - `anomaly.repeated_scenario_failure` (same scenario failing across multiple
    releases) usually signals external service degradation (e.g. 503 on
    security endpoints) or a host hook issue rather than a release-specific bug.
- `lane.overdue` means a job has run far longer than expected. Decide whether
  to `kill_job` it; the campaign then records that unit as errored and frees the lane.

Let plain failures accumulate and keep going. The point of a first pass is a
complete picture, not a green one. If an environmental bug breaks all lanes,
call `pause_campaign`, fix the root cause, and batch-retry.

## Stop, retry, resume

`pause_campaign` and `cancel_campaign` both drain: jobs already running finish
and are recorded. Neither kills anything. `lanes_busy` in the response says how
many are still draining. Cancel when the run is not worth continuing -- a
broken checkout, infrastructure that will not recover -- and say why. Pause a
run you mean to come back to: cancelling also closes the campaign to
`retry_units`, which is the whole difference between the two.

`reopen_campaign` takes a cancellation back, for one you made in error. The
cancel stays in the record and the reopen is appended after it. A campaign
with units left unattempted starts scheduling again; one whose units were all
attempted comes back `complete`, so reopening it is followed by `retry_units`,
not `start_campaign`. If the cancel left jobs in flight that no server is
watching any more, it names those units and refuses; check them, then reopen
with `abandon_in_flight` to record them as errored.

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

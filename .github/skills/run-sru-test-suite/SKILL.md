---
name: run-sru-test-suite
description: 'Run and track the Ubuntu Pro Client Behave test suite across releases and machine types for an SRU. Use when verifying the features/ integration suite for an SRU, or resuming an SRU verification already under way.'
argument-hint: 'Describe the SRU verification run'
---

# Run SRU Test Suite

The `behave` MCP server's campaign tools schedule, run, classify and record
every job. Do not loop `start_scenario` yourself. You decide what the server
does not: what is in scope (agreed with the user), whether a failure is real
or flaky and deserves another attempt, and whether the run is worth
continuing. Each tool's description carries its parameters, actions and
response shape. Diagnose individual failures per
[feature-test-runs](../feature-test-runs/SKILL.md), which carries what the
failure classes mean on this host.

## Define scope

1. Ask for a Launchpad SRU bug URL or numeric ID. The ID identifies the
   campaign locally; do not read or update Launchpad.
2. `list_dimensions`, and show the user those exact release and
   `machine_type` values. Do not translate aliases or invent values.
3. Ask whether this is a full run or a named set of feature files or
   scenarios.
4. `create_campaign` with the agreed scope, `install_from: proposed`, and a
   `max_lanes` the host can stand.
5. Report the unit count it returns and **get the user's confirmation before
   starting**.

If `create_campaign` rejects the scope, resolve it with the user; do not work
around it.

## Run it

1. `control_campaign` with `action: start`.
2. Tell the user they can watch from a terminal without you, from
   `tools/mcp_server_behave`:
   `uv run behave-campaign events --campaign-id <id> --repo-root <checkout> --follow`
3. Delegate the watching to a subagent so the poll loop stays out of this
   conversation. Its prompt is the contents of
   [references/monitor.md](references/monitor.md) with the campaign id
   filled in -- pass that file, do not paraphrase it. Act on its report:
   - campaign `complete` -> **Finish**.
   - a decision needed (host fix, retry, kill, pause) -> take it with the
     user's confirmation, then dispatch the monitor again.
   - still healthy -> relay the counts and dispatch the monitor again.

## Judgement

Let plain failures accumulate and keep going. The point of a first pass is a
complete picture, not a green one.

- `campaign_status` with `group_by: scenario` is the picture. The same
  scenario failed on every release reads as a defect or a shared dependency;
  on one release, as a flake or a release-specific bug.
- Triage each class once. Host configuration and infrastructure are
  actionable now; external flakes wait for the batch retry; candidate
  defects are recorded, never retried in hope.
- `unit.unclassifiable` is a finding to report, not an error to work around.
- `anomaly.repeated_scenario_failure` usually means a shared dependency
  or a host issue rather than a release bug.
- `lane.overdue`: decide whether to `kill_job` it.
- Before retrying a unit a second time, check `unit_history`; a unit that
  erred the same way twice is not a flake.
- If one cause is failing every lane, pause, fix it, then `retry_units` in
  a batch with a `reason`.

## Stop, retry, resume

`control_campaign`'s description is the decision table. **pause** a run you
mean to come back to; **cancel** one that is not worth continuing -- a broken
checkout, infrastructure that will not recover -- and say why.

A campaign the server was running when it last stopped comes back `paused`
with reason `server_restart`; check its in-flight units before resuming.

## Finish

Report without giving an overall verdict, from `campaign_status` with
`group_by: scenario` and a `problems_limit` above `problems_total`:

```markdown
## SRU <id> -- behave verification

Counts: <passed> passed, <failed> failed, <skipped> skipped, <error> error
of <total> units. Install source: <install_from>. Checkout: <branch>@<commit>.

### Problems by scenario
| Feature | Scenario | Failed | Skipped | Error |
| ... | ... | <release> on <machine_type> (job <id>) ... | ... | ... |

### Classes
- <class>: <which scenarios>, <what it means>, <retried? outcome>

Campaign id: <id>. Job ids above open with get_scenario_errors.
```

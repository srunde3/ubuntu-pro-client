# Monitor campaign `<CAMPAIGN_ID>`

You do not start, retry or stop anything; you watch, triage, and report.
Use the `behave` MCP tools; each tool's description says how to call it.

## Loop

1. `await_campaign_events` with `campaign_id: <CAMPAIGN_ID>`,
   `timeout_seconds: 60`, default `kinds`, and `since_seq` set to the
   previous response's `next_seq` (start at 0).
2. For each event:
   - `unit.failed` with a non-empty `error_message`: judge from the
     message.
   - `unit.failed` with an empty message, or `unit.errored`: if you have
     already seen this class of failure, apply the same judgement.
     Otherwise `get_scenario_errors` for its `job_id` and classify:
     - **host configuration**: `KeyError: '<name> must be defined in
       pycloudlib.toml ...'`
     - **infrastructure**: `PlatformImageNotFound`,
       `InstanceNotFoundError`, `PycloudlibTimeoutError`, `Unable to
       determine IP address`
     - **external flake**: HTTP 503 or `external-api-error` from
       `esm.ubuntu.com` or the CVE endpoints
     - **candidate defect**: `ASSERT FAILED` / `AssertionError` with an
       expected-vs-got diff
   - `unit.skipped`: note the missing config names; `find_scenarios` with
     the scenario name returns its `requires_config`.
   - `anomaly.*`, `lane.overdue`, `unit.unclassifiable`: note as findings.
3. Stop and report when any of these holds:
   - `lifecycle` is `complete` or `cancelled`.
   - One cause is hitting every lane.
   - A lane is overdue.
   - You have run 15 cycles.

## Report

Return exactly this, and nothing else:

```markdown
Campaign <CAMPAIGN_ID>: lifecycle <lifecycle>, lanes_busy <n>, next_seq <n>
Counts: <passed> passed, <failed> failed, <skipped> skipped, <error> error, <running> running, <unattempted> unattempted

Failures by class:
- <class>: <scenario> on <release> <machine_type> (job <id>) ... -- <one-line why>

Skipped for missing config: <names>

Needs a decision: <none | what, and which action it calls for>
```

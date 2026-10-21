---
name: feature-test-runs
description: "Run, discover, and debug the features/ behave integration tests through the behave MCP server. Use when asked to run a scenario or feature on a release or machine type, to find which scenarios cover something, or to explain why a behave job failed, errored, or was skipped -- a traceback, a provisioning error, a hook error, an assertion diff, a missing config."
---

# Feature Test Runs

Use the `behave` MCP server (`tools/mcp_server_behave`) for all `features/`
discovery and execution. Don't run `tox -e behave` by hand, and don't grep
`features/*.feature` for scenarios or valid release/machine_type values.
Each tool's description says how to call it and what it returns.

## Run

1. `list_dimensions` for the exact release and `machine_type` values;
   `list_features` or `find_scenarios` to pick what to run.
2. `run_job` for one unit -- a scenario's exact name, one release and
   one machine type it covers -- then `wait_for_job`. Start
   one job per unit; there is no multi-release or multi-machine-type job. A
   job whose pool is full is `queued` and starts by itself when a lane
   frees; a wait that runs out returns it with `timed_out: true`, so wait
   again.
3. For more than a handful of scenarios, use the campaign tools instead; see
   [run-sru-test-suite](../run-sru-test-suite/SKILL.md).

## Diagnose a failed job

Never read a whole log, through `job_logs` or from the path
`job_artifacts` gives. A job's log is 150-3,000 lines and the
cause is rarely in the tail.

1. If a failing step has a non-empty `error_message`, judge from it.
2. Otherwise -- `status: error`, an empty message, a unit that errored --
   `job_errors`. `finished: false` means behave never reached its
   summary: tox or pip failed, or the job was killed, so read `tail`.
3. Only if a region's capped text is not enough, `job_logs` around
   its `first_line`.

A job with `state: interrupted` did not fail. With `reason: killed`, someone
killed it; with `reason: died`, its process died before it could record an
exit -- a crash, a hard kill or a reboot. Nothing it did counts; start it
again.

Then say which class below it is and why.

## Gotchas: what the failures mean here

Classify a class once; the judgement applies to every unit that hit it. Do
not read each log.

- `KeyError: '<name> must be defined in pycloudlib.toml to make this call'`
  is **host configuration**, not the test: the machine type needs a cloud
  section in the host's `pycloudlib.toml`. Every unit on that machine type
  fails the same way. Fix the host, then rerun.
- `PlatformImageNotFound`, `InstanceNotFoundError`, `PycloudlibTimeoutError`
  and `Unable to determine IP address` are **infrastructure**: the cloud or
  LXD could not provide or reach the machine. A retry is reasonable once
  the cause is understood; a retired image (`18.04-DAILY-LTS`) will not
  come back.
- An HTTP 503 or `external-api-error` from `esm.ubuntu.com` or the CVE
  endpoints is an **external flake**. Let it accumulate and retry in a
  batch later; do not report it as a defect.
- `ASSERT FAILED` / `AssertionError` with an expected-vs-got diff is a
  **candidate defect** in the client or the test. Quote the diff; do not
  retry it hoping for a different answer.
- A **skipped** scenario means a config the host lacks. `requires_config`
  on the catalog entry names them; jobs get them only from the checkout's
  config (`behave-tests config set behave.<name>`), which the user sets.
  Report the option names. Never ask for a secret value in chat.

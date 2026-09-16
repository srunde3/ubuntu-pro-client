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
2. `start_scenario`, then `wait_for_scenario_completion`.
3. For more than a handful of scenarios, use the campaign tools instead; see
   [run-sru-test-suite](../run-sru-test-suite/SKILL.md).

## Diagnose a failed job

Never read a whole log, through `get_scenario_logs` or from the path
`get_scenario_artifacts` gives. A job's log is 150-3,000 lines and the
cause is rarely in the tail.

1. If a failing step has a non-empty `error_message`, judge from it.
2. Otherwise -- `status: error`, an empty message, a unit that errored --
   `get_scenario_errors`. `finished: false` means behave never reached its
   summary: tox or pip failed, or the job was killed, so read `tail`.
3. Only if a region's capped text is not enough, `get_scenario_logs` around
   its `first_line`.

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
  on the catalog entry names them; the value comes from
  `UACLIENT_BEHAVE_<NAME>` or `~/.config/protest.yaml`. Report the variable
  names. Never ask for a secret value in chat.

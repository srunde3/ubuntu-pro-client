---
name: behave-feature-tests
description: Run and troubleshoot Ubuntu Pro Client Behave feature tests, found in the `features/` directory.
---

# Behave Feature Tests

TODO: add context about how behave tests run

## Tools

- `tools/release_catalog.py` (Tool 1) — authoritative Ubuntu release catalog, backed by `distro-info`: chronological ordering, LTS flag, and current status (devel/supported/esm/legacy/eol) per release. Source of truth for "what releases exist and what's their status right now."
- `tools/release_tags.py` (Tool 2) — parses a scenario's `@releases.*` tags (see [dev-docs/reference/release_coverage_tags.md](../../../dev-docs/reference/release_coverage_tags.md)) into its declared `tracks`/`since`/`until`/`machine_types`/`exceptions`.
- `tools/coverage_gaps.py` (Tool 3) — combines Tools 1 and 2 with a scenario's current coverage (from the MCP's `describe_feature`) to compute `Missing(S)` per [dev-docs/explanation/release_coverage_model.md](../../../dev-docs/explanation/release_coverage_model.md). Never parses `.feature` files itself — all coverage data comes from the MCP; all applicability data comes from tags, not a side log.

## Using the MCP

You MUST use the behave MCP server for feature-file investigation (`list_features`, `describe_feature`, `find_scenarios`, `list_dimensions`) and for test execution (`start_behave_scenario`, `wait_for_scenario_completion`, `get_scenario_logs`, `get_scenario_artifacts`). If the MCP server is unavailable or cannot run a particular test (unsupported `machine_type`, missing credentials, etc.), you MUST raise this concern to the user before attempting anything else — do not fall back to running `tox`/`behave` directly.

When checking whether a run passed, check the actual scenario counts (`passed >= 1 and failed == 0`), not just the job's overall `ok` flag — behave exits successfully when every scenario is skipped, which is not a pass.

TODO: add note to the MCP about local builds/upstream builds

## Adding new Examples

When editing the `Examples` matrix, the existing precedent is the most important reference. Updating the `Examples` should not require any changes to the test body. If it does, you MUST raise this concern to the user. You MUST NOT edit the body of a test without consent from the user.

Before adding to `Examples`, run Tool 3 (`coverage_gaps.py`) to see whether a currently-relevant release is missing. It computes this from the scenario's `@releases.*` tags (Tool 2) against the release catalog (Tool 1) — see [dev-docs/explanation/release_coverage_model.md](../../../dev-docs/explanation/release_coverage_model.md) for how `Missing(S)` is derived. A scenario with no `@releases.*` tags is reported `UNCLASSIFIED`, not silently treated as covered or as a gap — tagging it is itself a needed step, not something to guess at.

For a real gap, there are exactly two acceptable outcomes:

1. **Add a row.** The example is relevant; add it following existing precedent (same `machine_type`s as the closest prior row for that track, unless the file's history says otherwise).
2. **Add an `@releases.skip.*` exception tag.** The example is deliberately not relevant for that specific release (and optionally machine_type). This MUST name only the one release it covers, with a reason in a comment on its own line above the tag — never an open-ended "not relevant from here on" claim, since that kind of claim silently goes stale as the product changes. The next release on the same track always needs its own decision, even if the previous one was skipped. See [dev-docs/reference/release_coverage_tags.md](../../../dev-docs/reference/release_coverage_tags.md) for the exact tag syntax.

Examples MUST be ordered by release, based on the release year. For example:

```
    Examples: version
      | release  | machine_type   |
      | xenial   | lxd-container  |
      | xenial   | lxd-vm         |
      | bionic   | lxd-container  |
      | bionic   | lxd-vm         |
      | focal    | lxd-container  |
      | focal    | lxd-vm         |
      | jammy    | lxd-container  |
      | jammy    | lxd-vm         |
      | noble    | lxd-container  |
      | noble    | lxd-vm         |
      | questing | lxd-container  |
      | questing | lxd-vm         |
      | resolute | lxd-container  |
      | resolute | lxd-vm         |
      | stonking | lxd-container  |
      | stonking | lxd-vm         |
```

After adding a new example, you MUST validate the change by running the test via the MCP to ensure it passes.

## Diagnosing test failures

You SHOULD attempt to identify if a test failure is due to a true failure in the Ubuntu Pro client or if the failure is due to a faulty test harness. This determination is a judgment call for the user, not something to resolve unilaterally — do not silently mark a scenario `@skip`, edit its body, or drop a row just to make a run pass.

## Checkpoints

TODO: enumerate the human-in-the-loop gates (scope approval before editing, body-change consent, "MCP can't run this", failure triage, commit review) once the workflow has been exercised end to end.

## Pitfalls

- You SHOULD use the git hook run `reformat-gherkin`; you SHOULD NOT reformat feature files by hand unless specifically requested. You SHOULD add the autoformatted changes and attempt the commit again.

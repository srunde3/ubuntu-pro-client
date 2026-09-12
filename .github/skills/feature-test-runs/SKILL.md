---
name: feature-test-runs
description: "Use when running, discovering, or debugging `features/` behave integration tests or scenarios."
---

# Feature Test Runs

Use the `behave` MCP server (`tools/mcp_server_behave`) for all `features/`
test discovery and execution. Don't run `tox -e behave` by hand, and don't
grep `features/*.feature` files directly to find scenarios or valid
release/machine_type values -- the MCP tools already do this.

Start with `list_features`, `find_scenarios`, or `list_dimensions` to
discover what's available, then `start_scenario` plus
`wait_for_scenario_completion`/`list_scenario_jobs`/
`summarize_scenario_results` to run scenarios and check results. Each
tool's own description covers its parameters and output shape -- read
those (or call `list_tools`) rather than looking here for details.

For more than a handful of scenarios -- an SRU verification, or anything you
would otherwise loop `start_scenario` over -- use the campaign tools instead
(`create_campaign`, `start_campaign`, `await_campaign_events`). They keep the
lanes full, record every outcome, and survive a restart, none of which a loop
of `start_scenario` calls does. See
[run-sru-test-suite](../run-sru-test-suite/SKILL.md).

See [tools/mcp_server_behave/README.md](../../../tools/mcp_server_behave/README.md)
if the MCP server isn't configured yet, or for env vars and safety
constraints (allowed machine types, cloud gating, parallel job limits).


---
name: feature-test-runs
description: "Use when deciding how and when to run `features/` behave integration tests."
---

# Feature Test Runs

TODO: will need to mention the MCP once it is running. This is a WIP skill.

Use this skill when a change touches integration behavior under `features/` or when you need to validate a change with behave.

## When to run

- Run a targeted `features/*.feature` test after changing CLI flows, cloud setup, packaging, AppArmor, or other integration paths.
- Prefer the smallest useful slice: a single feature, scenario line, release, or machine type.
- Use `UACLIENT_BEHAVE_INSTALL_FROM=local` when validating local code changes.
- Use `@wip` for new or unstable scenarios, then run only WIP scenarios with `tox -e behave -- -w`.

## How to run

- Single feature file: `tox -e behave -- features/unattached_commands.feature`
- Single scenario by line: `tox -e behave -- features/config.feature:132`
- Specific release and machine type: `tox -e behave -- features/config.feature -D releases=jammy -D machine_types=lxd-vm`
- Local code under test: `UACLIENT_BEHAVE_INSTALL_FROM=local tox -e behave -- features/cli/attach.feature -D releases=resolute -D machine_types=lxd-container,lxd-vm`

## Notes

- `features/` is the integration-test entry point for behave.
- Read [dev-docs/how-to/integration_testing.md](dev-docs/how-to/integration_testing.md) before adding new commands or conventions.
- Cloud-backed runs may require the right credentials and `UACLIENT_BEHAVE_CONTRACT_TOKEN`.
